/**
 * ZVEZDOCHKA BOOKING BRIDGE v5.9.2
 * Bound to one Google Sheet. Deploy as Web App (execute as owner).
 * The bot authenticates with API_SECRET stored in Script Properties.
 */

const SHEETS = {
  BOOKINGS: 'Бронирования',
  SERVICES: 'Объекты и услуги',
  BLOCKS: 'Блокировки',
  SETTINGS: 'Настройки',
  MANAGER: 'Бронирования — менеджер',
};

const BOOKING_HEADERS = [
  'booking_id','status','service_key','service_name','resource_key','resource_name',
  'start_at','end_at','full_name','phone','guest_count','vk_user_id','vk_peer_id',
  'source','comment','created_at','updated_at','expires_at','manager_id','price_amount','price_text'
];
const SERVICE_HEADERS = [
  'enabled','service_key','service_name','resource_key','resource_name','mode',
  'open_time','close_time','min_duration_minutes','max_duration_minutes','max_guests',
  'manager_approval','notes','price_mode','price_amount','price_unit','price_duration_minutes','price_includes'
];
const BLOCK_HEADERS = ['enabled','service_key','resource_key','start_at','end_at','reason'];
const SETTINGS_HEADERS = ['key','value','comment'];
const MANAGER_HEADERS = [
  'Статус','Услуга','Объект','Начало','Окончание','ФИО','Телефон','Гостей','Стоимость',
  'Создано','Обновлено','Комментарий','booking_id','service_key','resource_key','vk_user_id',
  'vk_peer_id','source','expires_at','manager_id'
];

function setupSpreadsheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('Откройте Apps Script из нужной Google Таблицы.');
  // The camp is in Udmurtia (UTC+4). Force the spreadsheet itself to the same
  // timezone so managers see exactly the same clock time that parents requested.
  ss.setSpreadsheetTimeZone('Europe/Samara');
  PropertiesService.getScriptProperties().setProperty('SPREADSHEET_ID', ss.getId());
  if (!PropertiesService.getScriptProperties().getProperty('API_SECRET')) {
    const secret = Utilities.getUuid().replace(/-/g,'') + Utilities.getUuid().replace(/-/g,'');
    PropertiesService.getScriptProperties().setProperty('API_SECRET', secret);
    console.log('BOOKING_API_SECRET=' + secret);
  }

  ensureSheet_(ss, SHEETS.BOOKINGS, BOOKING_HEADERS);
  const services = ensureSheet_(ss, SHEETS.SERVICES, SERVICE_HEADERS);
  const blocks = ensureSheet_(ss, SHEETS.BLOCKS, BLOCK_HEADERS);
  const settings = ensureSheet_(ss, SHEETS.SETTINGS, SETTINGS_HEADERS);
  const manager = ensureSheet_(ss, SHEETS.MANAGER, MANAGER_HEADERS);

  if (services.getLastRow() === 1) {
    services.getRange(2,1,2,SERVICE_HEADERS.length).setValues([
      [false,'gazebo','Беседка','gazebo-1','Беседка','hourly','','',0,0,0,true,'Заполните реальные часы, длительность и вместимость, затем включите строку.','fixed_duration',3300,'за 3 часа',180,'Мангал, уголь, розжиг и решётка'],
      [false,'corpus','Тур выходного дня','corpus-1','Корпус','date_range','','',0,0,0,true,'Заполните реальные правила и вместимость корпуса, затем включите строку.','per_person',1450,'с человека',0,'Проживание, ужин и завтрак'],
    ]);
  }
  if (settings.getLastRow() === 1) {
    settings.getRange(2,1,3,3).setValues([
      ['timezone','Europe/Samara','Часовой пояс лагеря'],
      ['hold_minutes','15','Сколько минут держать временную бронь'],
      ['version','5.9.2','Версия схемы'],
    ]);
  } else {
    const settingRows = rowsFromSheet_(settings);
    const versionRow = settingRows.find(r => String(r.key || '') === 'version');
    if (versionRow) settings.getRange(versionRow._row,2).setValue('5.9.2');
  }
  backfillOfficialRentalPricing_(services);
  services.setFrozenRows(1); blocks.setFrozenRows(1); settings.setFrozenRows(1); manager.setFrozenRows(1);
  const bookings = ss.getSheetByName(SHEETS.BOOKINGS);
  bookings.setFrozenRows(1);
  // Keep the manager-facing journal readable. Apps Script writes real Date values,
  // so these columns display in local camp time instead of raw ISO strings.
  [7,8,16,17,18].forEach(col => bookings.getRange(2,col,Math.max(bookings.getMaxRows()-1,1),1).setNumberFormat('dd.MM.yyyy HH:mm'));
  [4,5].forEach(col => blocks.getRange(2,col,Math.max(blocks.getMaxRows()-1,1),1).setNumberFormat('dd.MM.yyyy HH:mm'));
  [7,8].forEach(col => services.getRange(2,col,Math.max(services.getMaxRows()-1,1),1).setNumberFormat('HH:mm'));
  syncManagerSheet_();
  formatManagerSheet_(manager);
  console.log('SPREADSHEET_ID=' + ss.getId());
  console.log('Готово. Теперь Deploy -> New deployment -> Web app.');
}

function doGet() {
  return json_({ok:true, result:{service:'zvezdochka-booking', version:'5.9.2'}});
}

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const expected = PropertiesService.getScriptProperties().getProperty('API_SECRET') || '';
    if (!expected || String(body.secret || '') !== expected) return json_({ok:false,error:'unauthorized'});
    const action = String(body.action || '');
    const payload = body.payload && typeof body.payload === 'object' ? body.payload : {};
    if (!payload.request_id && body.request_id) payload.request_id = String(body.request_id);
    cleanupExpiredHolds_();
    let result;
    if (action === 'health') result = {healthy:true, version:'5.9.2'};
    else if (action === 'get_service') result = getService_(payload);
    else if (action === 'check_availability') result = checkAvailability_(payload);
    else if (action === 'create_hold') result = createHold_(payload);
    else if (action === 'submit_booking') result = submitBooking_(payload);
    else if (action === 'manager_decision') result = managerDecision_(payload);
    else if (action === 'cancel_booking') result = cancelBooking_(payload);
    else if (action === 'get_booking') result = getBooking_(payload);
    else throw new Error('unknown_action');
    return json_({ok:true,result:result});
  } catch (err) {
    console.error(err && err.stack ? err.stack : err);
    return json_({ok:false,error:String(err && err.message ? err.message : err)});
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function ss_() {
  const id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (!id) throw new Error('SPREADSHEET_ID is not configured; run setupSpreadsheet() first');
  return SpreadsheetApp.openById(id);
}

function ensureSheet_(ss, name, headers) {
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (sh.getLastRow() === 0) sh.getRange(1,1,1,headers.length).setValues([headers]);
  const current = sh.getRange(1,1,1,headers.length).getValues()[0];
  if (current.join('|') !== headers.join('|')) sh.getRange(1,1,1,headers.length).setValues([headers]);
  return sh;
}

function rowsFromSheet_(sh) {
  if (!sh || sh.getLastRow() < 2) return [];
  const values = sh.getDataRange().getValues();
  const headers = values.shift().map(String);
  return values.map((row, idx) => {
    const out = {_row: idx + 2};
    headers.forEach((h,i) => out[h] = row[i]);
    return out;
  });
}

function backfillOfficialRentalPricing_(services) {
  const rows = rowsFromSheet_(services);
  rows.forEach(r => {
    const key = String(r.service_key || '');
    const patch = {};
    if (key === 'gazebo') {
      if (!String(r.price_mode || '').trim()) patch.price_mode = 'fixed_duration';
      if (!Number(r.price_amount || 0)) patch.price_amount = 3300;
      if (!String(r.price_unit || '').trim()) patch.price_unit = 'за 3 часа';
      if (!Number(r.price_duration_minutes || 0)) patch.price_duration_minutes = 180;
      if (!String(r.price_includes || '').trim()) patch.price_includes = 'Мангал, уголь, розжиг и решётка';
    } else if (key === 'corpus') {
      if (!String(r.service_name || '').trim() || String(r.service_name) === 'Аренда корпуса') patch.service_name = 'Тур выходного дня';
      if (!String(r.price_mode || '').trim()) patch.price_mode = 'per_person';
      if (!Number(r.price_amount || 0)) patch.price_amount = 1450;
      if (!String(r.price_unit || '').trim()) patch.price_unit = 'с человека';
      if (!String(r.price_includes || '').trim()) patch.price_includes = 'Проживание, ужин и завтрак';
    }
    if (Object.keys(patch).length) updateObjectRow_(SHEETS.SERVICES, SERVICE_HEADERS, r._row, patch);
  });
}

function statusRu_(status) {
  const map = {hold:'Временная бронь',pending_manager:'Ожидает подтверждения',confirmed:'Подтверждено',rejected:'Отклонено',cancelled:'Отменено',expired:'Истекло'};
  return map[String(status || '')] || String(status || '');
}

function formatPriceForManager_(r) {
  if (String(r.price_text || '').trim()) return String(r.price_text).replace(/^💳\s*/, '');
  const amount = Number(r.price_amount || 0);
  return amount ? amount + ' ₽' : '';
}

function formatManagerSheet_(sh) {
  sh.setFrozenRows(1);
  sh.getRange(1,1,1,MANAGER_HEADERS.length).setFontWeight('bold');
  [4,5,10,11,19].forEach(col => sh.getRange(2,col,Math.max(sh.getMaxRows()-1,1),1).setNumberFormat('dd.MM.yyyy HH:mm'));
  sh.autoResizeColumns(1,12);
  try { sh.hideColumns(13,8); } catch (e) {}
}

function syncManagerSheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(SHEETS.MANAGER);
  let created = false;
  if (!sh) { sh = ss.insertSheet(SHEETS.MANAGER); created = true; }
  if (sh.getMaxColumns() < MANAGER_HEADERS.length) sh.insertColumnsAfter(sh.getMaxColumns(), MANAGER_HEADERS.length - sh.getMaxColumns());
  sh.getRange(1,1,1,MANAGER_HEADERS.length).setValues([MANAGER_HEADERS]);
  const previousLastRow = sh.getLastRow();
  const bookings = rows_(SHEETS.BOOKINGS);
  const values = bookings.map(r => [
    statusRu_(r.status),String(r.service_name || ''),String(r.resource_name || ''),r.start_at || '',r.end_at || '',
    String(r.full_name || ''),String(r.phone || ''),Number(r.guest_count || 0) || '',formatPriceForManager_(r),
    r.created_at || '',r.updated_at || '',String(r.comment || ''),String(r.booking_id || ''),String(r.service_key || ''),
    String(r.resource_key || ''),String(r.vk_user_id || ''),String(r.vk_peer_id || ''),String(r.source || ''),r.expires_at || '',String(r.manager_id || '')
  ]);
  // Clear only rows that were actually used. Clearing the default ~1000-row sheet on every
  // hold/submit/manager click adds needless Apps Script latency to the parent booking flow.
  if (previousLastRow > 1) sh.getRange(2,1,previousLastRow - 1,MANAGER_HEADERS.length).clearContent();
  if (values.length) sh.getRange(2,1,values.length,MANAGER_HEADERS.length).setValues(values);
  if (created) formatManagerSheet_(sh);
}

function rows_(sheetName) {
  const sh = ss_().getSheetByName(sheetName);
  if (!sh || sh.getLastRow() < 2) return [];
  const values = sh.getDataRange().getValues();
  const headers = values.shift().map(String);
  return values.map((row, idx) => {
    const out = {_row: idx + 2};
    headers.forEach((h,i) => out[h] = row[i]);
    return out;
  });
}

function appendObject_(sheetName, headers, obj) {
  const sh = ss_().getSheetByName(sheetName);
  sh.appendRow(headers.map(h => obj[h] === undefined ? '' : obj[h]));
}

function updateObjectRow_(sheetName, headers, rowNumber, patch) {
  const sh = ss_().getSheetByName(sheetName);
  const values = sh.getRange(rowNumber,1,1,headers.length).getValues()[0];
  headers.forEach((h,i) => { if (Object.prototype.hasOwnProperty.call(patch,h)) values[i] = patch[h]; });
  sh.getRange(rowNumber,1,1,headers.length).setValues([values]);
}

function truthy_(v) { return v === true || String(v).toLowerCase() === 'true' || String(v) === '1'; }
function iso_(v) { const d = new Date(v); if (isNaN(d.getTime())) throw new Error('invalid_datetime'); return d; }

function setting_(key, fallbackValue) {
  const rows = rows_(SHEETS.SETTINGS);
  const hit = rows.find(r => String(r.key) === String(key));
  return hit ? String(hit.value) : fallbackValue;
}

function hm_(value, tz) {
  if (value instanceof Date && !isNaN(value.getTime())) return Utilities.formatDate(value, tz, 'HH:mm');
  const s = String(value === undefined || value === null ? '' : value).trim();
  const m = s.match(/^([01]?\d|2[0-3]):([0-5]\d)$/);
  return m ? ('0' + Number(m[1])).slice(-2) + ':' + m[2] : '';
}

function validServiceRow_(r, tz) {
  const mode = String(r.mode || 'hourly');
  const resourceKey = String(r.resource_key || '').trim();
  const serviceKey = String(r.service_key || '').trim();
  const capacity = Number(r.max_guests || 0);
  if (!serviceKey || !resourceKey || !String(r.service_name || '').trim() || !String(r.resource_name || '').trim()) return false;
  if (!Number.isFinite(capacity) || capacity <= 0) return false;
  if (mode === 'date_range') return true;
  if (mode !== 'hourly') return false;
  const open = hm_(r.open_time, tz), close = hm_(r.close_time, tz);
  const minDuration = Number(r.min_duration_minutes || 0), maxDuration = Number(r.max_duration_minutes || 0);
  return !!open && !!close && open < close && minDuration > 0 && maxDuration >= minDuration;
}

function serviceRows_(serviceKey) {
  const tz = setting_('timezone', 'Europe/Samara');
  return rows_(SHEETS.SERVICES).filter(r =>
    String(r.service_key) === String(serviceKey) && truthy_(r.enabled) && validServiceRow_(r, tz)
  );
}

function getService_(payload) {
  const key = String(payload.service_key || '');
  const candidates = serviceRows_(key);
  if (!candidates.length) return {found:false,enabled:false,service_key:key};
  const r = candidates[0];
  const capacities = candidates.map(x => Number(x.max_guests || 0)).filter(x => x > 0);
  const maxGuests = capacities.length ? Math.max.apply(null, capacities) : 0;
  return {
    found:true, enabled:true, service_key:key, service_name:String(r.service_name || key),
    resource_key:candidates.length === 1 ? String(r.resource_key || '') : '',
    resource_name:candidates.length === 1 ? String(r.resource_name || '') : '',
    mode:String(r.mode || 'hourly'), open_time:hm_(r.open_time, setting_('timezone', 'Europe/Samara')), close_time:hm_(r.close_time, setting_('timezone', 'Europe/Samara')),
    min_duration_minutes:Number(r.min_duration_minutes || 0), max_duration_minutes:Number(r.max_duration_minutes || 0),
    max_guests:maxGuests, manager_approval:truthy_(r.manager_approval), notes:String(r.notes || ''),
    price_mode:String(r.price_mode || ''), price_amount:Number(r.price_amount || 0), price_unit:String(r.price_unit || ''),
    price_duration_minutes:Number(r.price_duration_minutes || 0), price_includes:String(r.price_includes || '')
  };
}

function activeStatus_(s) {
  return ['hold','pending_manager','confirmed'].indexOf(String(s)) >= 0;
}

function overlaps_(aStart,aEnd,bStart,bEnd) {
  return aStart.getTime() < bEnd.getTime() && bStart.getTime() < aEnd.getTime();
}

function checkAvailability_(payload) {
  const serviceKey = String(payload.service_key || '');
  const candidates = serviceRows_(serviceKey);
  if (!candidates.length) return {available:false,reason:'service_disabled'};
  const start = iso_(payload.start_at), end = iso_(payload.end_at);
  if (end <= start) return {available:false,reason:'invalid_interval'};

  const durationMinutes = Math.round((end.getTime() - start.getTime()) / 60000);
  const tz = setting_('timezone', 'Europe/Samara');

  function resourceAvailable_(serviceRow) {
    const resourceKey = String(serviceRow.resource_key || '');
    const mode = String(serviceRow.mode || 'hourly');
    const minDuration = Number(serviceRow.min_duration_minutes || 0);
    const maxDuration = Number(serviceRow.max_duration_minutes || 0);
    const capacity = Number(serviceRow.max_guests || 0);
    const guestCount = Number(payload.guest_count || 0);
    if (capacity && guestCount && guestCount > capacity) return {ok:false, reason:'capacity'};
    if (minDuration && durationMinutes < minDuration) return {ok:false, reason:'too_short'};
    if (maxDuration && durationMinutes > maxDuration) return {ok:false, reason:'too_long'};

    const startDay = Utilities.formatDate(start, tz, 'yyyy-MM-dd');
    const todayDay = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');
    if (mode === 'date_range' && startDay < todayDay) return {ok:false, reason:'past'};
    if (mode === 'hourly') {
      if (start.getTime() <= Date.now()) return {ok:false, reason:'past'};
      const endDay = Utilities.formatDate(new Date(end.getTime() - 1000), tz, 'yyyy-MM-dd');
      if (startDay !== endDay) return {ok:false, reason:'crosses_day'};
      const startHm = Utilities.formatDate(start, tz, 'HH:mm');
      const endHm = Utilities.formatDate(end, tz, 'HH:mm');
      const open = hm_(serviceRow.open_time, tz);
      const close = hm_(serviceRow.close_time, tz);
      if (startHm < open) return {ok:false, reason:'before_open'};
      if (endHm > close) return {ok:false, reason:'after_close'};
    }

    const conflicts = [];
    rows_(SHEETS.BOOKINGS).forEach(r => {
      if (!activeStatus_(r.status) || String(r.service_key) !== serviceKey) return;
      if (String(r.resource_key || '') !== resourceKey) return;
      if (payload.ignore_booking_id && String(r.booking_id) === String(payload.ignore_booking_id)) return;
      const rs = new Date(r.start_at), re = new Date(r.end_at);
      if (!isNaN(rs) && !isNaN(re) && overlaps_(start,end,rs,re)) conflicts.push(String(r.booking_id));
    });
    rows_(SHEETS.BLOCKS).forEach(r => {
      if (!truthy_(r.enabled) || String(r.service_key) !== serviceKey) return;
      const blockedResource = String(r.resource_key || '');
      if (blockedResource && blockedResource !== resourceKey) return;
      const rs = new Date(r.start_at), re = new Date(r.end_at);
      if (!isNaN(rs) && !isNaN(re) && overlaps_(start,end,rs,re)) conflicts.push('BLOCK:' + String(r.reason || 'blocked'));
    });
    return {ok:conflicts.length === 0, conflicts:conflicts, resource_key:resourceKey, resource_name:String(serviceRow.resource_name || '')};
  }

  let lastReason = 'occupied';
  let allConflicts = [];
  for (const serviceRow of candidates) {
    if (payload.resource_key && String(payload.resource_key) !== String(serviceRow.resource_key || '')) continue;
    const checked = resourceAvailable_(serviceRow);
    if (checked.ok) return {available:true, resource_key:checked.resource_key, resource_name:checked.resource_name};
    if (checked.reason) lastReason = checked.reason;
    if (checked.conflicts) allConflicts = allConflicts.concat(checked.conflicts);
  }
  return {available:false,reason:lastReason,conflicts:allConflicts};
}

function createHold_(payload) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(8000)) throw new Error('busy_retry');
  try {
    const requestId = String(payload.request_id || '').trim();
    if (requestId) {
      const marker = 'request_id:' + requestId;
      const existing = rows_(SHEETS.BOOKINGS).find(r => String(r.comment || '') === marker);
      if (existing) {
        const active = activeStatus_(existing.status);
        return {
          available:active,
          booking_id:String(existing.booking_id || ''),
          expires_at:existing.expires_at ? new Date(existing.expires_at).toISOString() : '',
          resource_key:String(existing.resource_key || ''),
          resource_name:String(existing.resource_name || ''),
          already:true,
          reason:active ? '' : 'request_already_processed'
        };
      }
    }
    const availability = checkAvailability_(payload);
    if (!availability.available) return {available:false,reason:String(availability.reason || 'occupied'),conflicts:availability.conflicts,alternatives:[]};
    const service = getService_({service_key:payload.service_key});
    const resourceKey = String(availability.resource_key || service.resource_key || '');
    const resourceName = String(availability.resource_name || service.resource_name || '');
    const now = new Date();
    const ttl = Math.max(5, Math.min(Number(payload.ttl_minutes || 15), 60));
    const expires = new Date(now.getTime() + ttl * 60000);
    const bookingId = 'ZV-' + Utilities.getUuid().split('-')[0].toUpperCase();
    appendObject_(SHEETS.BOOKINGS, BOOKING_HEADERS, {
      booking_id:bookingId,status:'hold',service_key:service.service_key,service_name:service.service_name,
      resource_key:resourceKey,resource_name:resourceName,start_at:new Date(payload.start_at),
      end_at:new Date(payload.end_at),full_name:'',phone:'',guest_count:Number(payload.guest_count || 0),
      vk_user_id:String(payload.vk_user_id || ''),vk_peer_id:String(payload.vk_peer_id || ''),source:'VK bot',
      comment:requestId ? ('request_id:' + requestId) : '',
      created_at:now,updated_at:now,expires_at:expires,manager_id:'',price_amount:'',price_text:''
    });
    syncManagerSheet_();
    return {available:true,booking_id:bookingId,expires_at:expires.toISOString(),resource_key:resourceKey,resource_name:resourceName};
  } finally { SpreadsheetApp.flush(); lock.releaseLock(); }
}

function findBooking_(bookingId) {
  return rows_(SHEETS.BOOKINGS).find(r => String(r.booking_id) === String(bookingId)) || null;
}

function submitBooking_(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(8000);
  try {
    const row = findBooking_(payload.booking_id);
    if (!row) return {submitted:false,message:'Заявка не найдена.'};
    if (['pending_manager','confirmed'].indexOf(String(row.status)) >= 0) {
      return {submitted:true,booking_id:String(row.booking_id),already:true};
    }
    if (String(row.status) !== 'hold') return {submitted:false,message:'Временная бронь уже неактивна.'};
    const exp = new Date(row.expires_at);
    if (!isNaN(exp) && exp.getTime() < Date.now()) {
      updateObjectRow_(SHEETS.BOOKINGS,BOOKING_HEADERS,row._row,{status:'expired',updated_at:new Date()});
      syncManagerSheet_();
      return {submitted:false,message:'Время временной брони истекло.'};
    }
    const fullName = String(payload.full_name || '').trim();
    const phone = String(payload.phone || '').trim();
    const guestCount = Number(payload.guest_count || row.guest_count || 0);
    if (!fullName || !phone || guestCount <= 0) return {submitted:false,message:'Не заполнены обязательные данные заявки.'};
    // Revalidate the exact held resource before accepting changed contact/party data.
    // This catches a capacity change, a newly-added manual block or a service that
    // was disabled after the hold was created. The hold itself is ignored by ID.
    const stillValid = checkAvailability_({
      service_key:String(row.service_key || ''), resource_key:String(row.resource_key || ''),
      start_at:new Date(row.start_at).toISOString(), end_at:new Date(row.end_at).toISOString(),
      guest_count:guestCount, ignore_booking_id:String(row.booking_id || '')
    });
    if (!stillValid.available) {
      updateObjectRow_(SHEETS.BOOKINGS,BOOKING_HEADERS,row._row,{
        status:'cancelled',updated_at:new Date(),expires_at:'',
        comment:'submit_revalidation:' + String(stillValid.reason || 'unavailable')
      });
      syncManagerSheet_();
      return {submitted:false,message:'Условия бронирования изменились. Нужно заново проверить дату, время и количество гостей.'};
    }
    updateObjectRow_(SHEETS.BOOKINGS,BOOKING_HEADERS,row._row,{
      status:'pending_manager',full_name:fullName,phone:phone,
      guest_count:guestCount,comment:String(payload.comment || ''),updated_at:new Date(),expires_at:'',
      price_amount:payload.price_amount === '' ? '' : Number(payload.price_amount || 0),price_text:String(payload.price_text || '')
    });
    syncManagerSheet_();
    return {submitted:true,booking_id:String(row.booking_id)};
  } finally { SpreadsheetApp.flush(); lock.releaseLock(); }
}

function managerDecision_(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(8000);
  try {
    const row = findBooking_(payload.booking_id);
    if (!row) return {updated:false,message:'Заявка не найдена.'};
    const next = String(payload.decision) === 'confirmed' ? 'confirmed' : 'rejected';
    if (String(row.status) === next) {
      return {
        updated:true,already:true,booking_id:String(row.booking_id),status:next,
        vk_user_id:String(row.vk_user_id || ''),vk_peer_id:String(row.vk_peer_id || ''),
        service_name:String(row.service_name || ''),resource_name:String(row.resource_name || ''),
        start_at:String(row.start_at || ''),end_at:String(row.end_at || '')
      };
    }
    if (String(row.status) !== 'pending_manager') return {updated:false,message:'Заявка уже обработана.'};
    if (next === 'confirmed') {
      const stillFree = checkAvailability_({
        service_key:String(row.service_key || ''), resource_key:String(row.resource_key || ''),
        start_at:new Date(row.start_at).toISOString(), end_at:new Date(row.end_at).toISOString(),
        guest_count:Number(row.guest_count || 0), ignore_booking_id:String(row.booking_id || '')
      });
      if (!stillFree.available) return {updated:false,message:'Перед подтверждением обнаружен конфликт в расписании. Проверьте таблицу.'};
    }
    updateObjectRow_(SHEETS.BOOKINGS,BOOKING_HEADERS,row._row,{status:next,updated_at:new Date(),manager_id:String(payload.manager_id || '')});
    syncManagerSheet_();
    return {
      updated:true,booking_id:String(row.booking_id),status:next,vk_user_id:String(row.vk_user_id || ''),vk_peer_id:String(row.vk_peer_id || ''),
      service_name:String(row.service_name || ''),resource_name:String(row.resource_name || ''),start_at:String(row.start_at || ''),end_at:String(row.end_at || '')
    };
  } finally { SpreadsheetApp.flush(); lock.releaseLock(); }
}

function cancelBooking_(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(8000);
  try {
    const row = findBooking_(payload.booking_id);
    if (!row) return {cancelled:false};
    if (['cancelled','rejected','expired'].indexOf(String(row.status)) >= 0) return {cancelled:true,already:true};
    updateObjectRow_(SHEETS.BOOKINGS,BOOKING_HEADERS,row._row,{status:'cancelled',comment:String(payload.reason || row.comment || ''),updated_at:new Date(),expires_at:''});
    syncManagerSheet_();
    return {cancelled:true};
  } finally { SpreadsheetApp.flush(); lock.releaseLock(); }
}

function getBooking_(payload) {
  const row = findBooking_(payload.booking_id);
  if (!row) return {found:false};
  delete row._row;
  return {found:true,booking:row};
}

function cleanupExpiredHolds_() {
  const now = Date.now();
  let changed = false;
  rows_(SHEETS.BOOKINGS).forEach(r => {
    if (String(r.status) !== 'hold' || !r.expires_at) return;
    const exp = new Date(r.expires_at);
    if (!isNaN(exp) && exp.getTime() < now) {
      updateObjectRow_(SHEETS.BOOKINGS,BOOKING_HEADERS,r._row,{status:'expired',updated_at:new Date()});
      changed = true;
    }
  });
  if (changed) syncManagerSheet_();
}
