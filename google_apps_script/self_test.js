'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

function assert(condition, message) {
  if (!condition) throw new Error(message || 'assertion failed');
}

class Range {
  constructor(sheet, row, col, numRows, numCols) {
    this.sheet = sheet; this.row = row; this.col = col; this.numRows = numRows; this.numCols = numCols;
  }
  getValues() {
    const out = [];
    for (let r = 0; r < this.numRows; r++) {
      const row = [];
      for (let c = 0; c < this.numCols; c++) row.push(this.sheet.get(this.row + r, this.col + c));
      out.push(row);
    }
    return out;
  }
  setValues(values) {
    for (let r = 0; r < this.numRows; r++) for (let c = 0; c < this.numCols; c++) this.sheet.set(this.row + r, this.col + c, values[r][c]);
    return this;
  }
  setValue(value) { this.sheet.set(this.row, this.col, value); return this; }
  setNumberFormat() { return this; }
  setFontWeight() { return this; }
  clearContent() {
    for (let r = 0; r < this.numRows; r++) for (let c = 0; c < this.numCols; c++) this.sheet.set(this.row + r, this.col + c, '');
    return this;
  }
}

class Sheet {
  constructor(name) { this.name = name; this.data = []; this.maxRows = 1000; }
  getLastRow() {
    let last = 0;
    this.data.forEach((row, i) => { if ((row || []).some(v => v !== '' && v !== null && v !== undefined)) last = i + 1; });
    return last;
  }
  getMaxRows() { return this.maxRows; }
  getMaxColumns() { return Math.max(26, ...this.data.map(r => (r || []).length)); }
  insertColumnsAfter() { return this; }
  getRange(row, col, numRows = 1, numCols = 1) { return new Range(this, row, col, numRows, numCols); }
  getDataRange() {
    const rows = Math.max(this.getLastRow(), 1);
    const cols = Math.max(1, ...this.data.map(r => (r || []).length));
    return new Range(this, 1, 1, rows, cols);
  }
  appendRow(row) { this.data.push([...row]); }
  setFrozenRows() { return this; }
  autoResizeColumns() { return this; }
  hideColumns() { return this; }
  get(row, col) { return (this.data[row - 1] || [])[col - 1] ?? ''; }
  set(row, col, value) {
    while (this.data.length < row) this.data.push([]);
    while (this.data[row - 1].length < col) this.data[row - 1].push('');
    this.data[row - 1][col - 1] = value;
  }
}

class Spreadsheet {
  constructor() { this.id = 'TEST-SHEET'; this.sheets = {}; this.timeZone = 'UTC'; }
  getId() { return this.id; }
  getSheetByName(name) { return this.sheets[name] || null; }
  insertSheet(name) { this.sheets[name] = new Sheet(name); return this.sheets[name]; }
  setSpreadsheetTimeZone(value) { this.timeZone = value; return this; }
}

const spreadsheet = new Spreadsheet();
const properties = {};
let uuidCounter = 1;

function formatDate(date, timeZone, pattern) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
    }).formatToParts(date).filter(x => x.type !== 'literal').map(x => [x.type, x.value])
  );
  if (pattern === 'yyyy-MM-dd') return `${parts.year}-${parts.month}-${parts.day}`;
  if (pattern === 'HH:mm') return `${parts.hour}:${parts.minute}`;
  throw new Error(`unsupported format ${pattern}`);
}

const context = {
  console: {log: () => {}, error: () => {}}, Date, JSON, Math, String, Number, Object, Array, isNaN,
  SpreadsheetApp: {
    flush: () => {},
    getActiveSpreadsheet: () => spreadsheet,
    openById: id => { assert(id === spreadsheet.id, 'unexpected spreadsheet id'); return spreadsheet; },
  },
  PropertiesService: {
    getScriptProperties: () => ({
      setProperty: (key, value) => { properties[key] = value; },
      getProperty: key => properties[key] || null,
    }),
  },
  Utilities: {
    getUuid: () => `0000000${uuidCounter++}-1111-2222-3333-444444444444`,
    formatDate,
  },
  LockService: {
    getScriptLock: () => ({ tryLock: () => true, waitLock: () => true, releaseLock: () => {} }),
  },
  ContentService: {
    MimeType: {JSON: 'JSON'},
    createTextOutput: body => ({body, setMimeType() { return this; }}),
  },
};

vm.createContext(context);
const code = fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8');
vm.runInContext(code, context);

context.setupSpreadsheet();
assert(properties.API_SECRET, 'setup did not generate API secret');
assert(spreadsheet.timeZone === 'Europe/Samara', 'spreadsheet timezone must be Europe/Samara');

const services = spreadsheet.getSheetByName('Объекты и услуги');
services.set(2, 1, true);

function post(action, payload = {}, secret = properties.API_SECRET) {
  const output = context.doPost({postData: {contents: JSON.stringify({secret, action, payload})}});
  return JSON.parse(output.body);
}

// Enabled but incomplete rows must fail closed.
let service = post('get_service', {service_key: 'gazebo'});
assert(service.ok && !service.result.enabled, 'incomplete service must not become bookable');

// Google Sheets may return time-only cells as Date objects. Exercise that path.
services.set(2, 7, new Date('2030-01-01T06:00:00Z')); // 10:00 Europe/Samara
services.set(2, 8, new Date('2030-01-01T18:00:00Z')); // 22:00 Europe/Samara
services.set(2, 9, 60);
services.set(2, 10, 720);
services.set(2, 11, 50);

service = post('get_service', {service_key: 'gazebo'});
assert(service.ok && service.result.enabled, 'configured gazebo should be enabled');
assert(service.result.open_time === '10:00' && service.result.close_time === '22:00', 'opening hours normalization failed');
assert(service.result.price_amount === 3300 && service.result.price_unit === 'за 3 часа', 'gazebo price migration failed');

const health = post('health');
assert(health.ok && health.result.healthy, 'health failed');
assert(post('health', {}, 'wrong-secret').ok === false, 'bad secret must be rejected');

const past = post('create_hold', {
  service_key: 'gazebo', start_at: '2020-01-01T14:00:00+04:00', end_at: '2020-01-01T16:00:00+04:00', guest_count: 10
});
assert(!past.result.available && past.result.reason === 'past', 'past time must be rejected');

const tooMany = post('create_hold', {
  service_key: 'gazebo', start_at: '2030-10-12T14:00:00+04:00', end_at: '2030-10-12T16:00:00+04:00', guest_count: 99
});
assert(!tooMany.result.available && tooMany.result.reason === 'capacity', 'capacity must be enforced');

const hold1 = post('create_hold', {
  service_key: 'gazebo', start_at: '2030-10-12T14:00:00+04:00', end_at: '2030-10-12T17:00:00+04:00',
  guest_count: 15, vk_user_id: 1, vk_peer_id: 1, ttl_minutes: 15,
});
assert(hold1.result.available && hold1.result.booking_id, 'first hold failed');

// Retrying the same create_hold after a lost ContentService redirect must not
// create a duplicate row. The bridge de-duplicates by request_id.
const retryPayload = {
  service_key: 'gazebo', start_at: '2030-10-12T20:00:00+04:00', end_at: '2030-10-12T22:00:00+04:00',
  guest_count: 10, vk_user_id: 3, vk_peer_id: 3, ttl_minutes: 15, request_id: 'REQ-IDEMPOTENT-1',
};
const retryHold1 = post('create_hold', retryPayload);
const retryHold2 = post('create_hold', retryPayload);
assert(retryHold1.result.available && retryHold2.result.available, 'idempotent hold retry failed');
assert(retryHold1.result.booking_id === retryHold2.result.booking_id, 'create_hold retry created a duplicate booking');
assert(retryHold2.result.already === true, 'duplicate create_hold was not marked already');

const hold2 = post('create_hold', {
  service_key: 'gazebo', start_at: '2030-10-12T16:00:00+04:00', end_at: '2030-10-12T18:00:00+04:00',
  guest_count: 10, vk_user_id: 2, vk_peer_id: 2, ttl_minutes: 15,
});
assert(!hold2.result.available && hold2.result.reason === 'occupied', 'overlapping hold must be rejected');

// Party size is revalidated again at submit time. A changed capacity must fail
// closed instead of promoting a now-invalid hold to pending_manager.
services.set(2, 11, 10);
const rejectedSubmit = post('submit_booking', {
  booking_id: hold1.result.booking_id, full_name: 'Иванов Иван', phone: '+79123456789', guest_count: 15,
});
assert(!rejectedSubmit.result.submitted, 'submit must revalidate capacity');
services.set(2, 11, 50);

const hold3 = post('create_hold', {
  service_key: 'gazebo', start_at: '2030-10-12T18:00:00+04:00', end_at: '2030-10-12T20:00:00+04:00',
  guest_count: 15, vk_user_id: 1, vk_peer_id: 1, ttl_minutes: 15,
});
assert(hold3.result.available && hold3.result.booking_id, 'second valid hold failed');
const submit = post('submit_booking', {
  booking_id: hold3.result.booking_id, full_name: 'Иванов Иван', phone: '+79123456789', guest_count: 15,
  price_amount: 3300, price_text: '💳 К ОПЛАТЕ: 3 300 ₽',
});
assert(submit.result.submitted, 'submit failed');

const decision = post('manager_decision', {booking_id: hold3.result.booking_id, decision: 'confirmed', manager_id: 9});
assert(decision.result.updated && decision.result.status === 'confirmed', 'manager confirmation failed');

const managerSheet = spreadsheet.getSheetByName('Бронирования — менеджер');
assert(managerSheet, 'manager-friendly booking sheet was not created');
const managerHeaders = managerSheet.getRange(1,1,1,20).getValues()[0];
assert(managerHeaders[0] === 'Статус' && managerHeaders[5] === 'ФИО' && managerHeaders[8] === 'Стоимость', 'manager sheet headers are not Russian/user-friendly');
const managerRows = managerSheet.getDataRange().getValues();
assert(managerRows.some(r => r[0] === 'Подтверждено' && String(r[8]).includes('3 300')), 'manager sheet did not sync confirmed booking/price');

// Multiple gazebo resources must behave as a shared pool: if gazebo-1 is occupied,
// the same service/time should be placed into another free resource.
services.getRange(4,1,1,13).setValues([[
  true,'gazebo','Беседка','gazebo-2','Беседка №2','hourly',
  new Date('2030-01-01T06:00:00Z'),new Date('2030-01-01T18:00:00Z'),60,720,50,true,'test resource 2'
]]);
const pooledService = post('get_service', {service_key: 'gazebo'});
assert(pooledService.result.enabled && pooledService.result.resource_key === '', 'multi-resource service must expose a resource pool');
const pooledHold = post('create_hold', {
  service_key: 'gazebo', start_at: '2030-10-12T18:00:00+04:00', end_at: '2030-10-12T20:00:00+04:00',
  guest_count: 10, vk_user_id: 4, vk_peer_id: 4, ttl_minutes: 15, request_id: 'REQ-POOL-1',
});
assert(pooledHold.result.available, 'pooled gazebo should find another free resource');
assert(pooledHold.result.resource_key === 'gazebo-2', 'pooled gazebo did not choose gazebo-2 when gazebo-1 was occupied');

// Running setup on an already-populated production sheet is the upgrade path for v5.9.2.
// It must add the manager view/pricing columns without deleting existing bookings.
const techBookings = spreadsheet.getSheetByName('Бронирования');
const rowsBeforeUpgradeRerun = techBookings.getLastRow();
context.setupSpreadsheet();
assert(techBookings.getLastRow() === rowsBeforeUpgradeRerun, 'setup rerun deleted or added technical booking rows');
const preserved = post('get_booking', {booking_id: hold3.result.booking_id});
assert(preserved.result.found && preserved.result.booking.status === 'confirmed', 'setup rerun damaged an existing confirmed booking');
const versionSetting = spreadsheet.getSheetByName('Настройки').getDataRange().getValues().find(r => r[0] === 'version');
assert(versionSetting && versionSetting[1] === '5.9.2', 'schema version was not upgraded in Settings');


// Exact v5.9.1 -> v5.9.2 migration smoke: production already has 19-column
// bookings and 13-column services. setupSpreadsheet() must extend those sheets
// in place without deleting/reordering existing rows.
spreadsheet.sheets = {};
spreadsheet.timeZone = 'UTC';
delete properties.SPREADSHEET_ID;

const legacyBookingHeaders = [
  'booking_id','status','service_key','service_name','resource_key','resource_name',
  'start_at','end_at','full_name','phone','guest_count','vk_user_id','vk_peer_id',
  'source','comment','created_at','updated_at','expires_at','manager_id'
];
const legacyServiceHeaders = [
  'enabled','service_key','service_name','resource_key','resource_name','mode',
  'open_time','close_time','min_duration_minutes','max_duration_minutes','max_guests',
  'manager_approval','notes'
];
const legacySettingsHeaders = ['key','value','comment'];
const legacyBlockHeaders = ['enabled','service_key','resource_key','start_at','end_at','reason'];

const legacyBookings = spreadsheet.insertSheet('Бронирования');
legacyBookings.appendRow(legacyBookingHeaders);
legacyBookings.appendRow([
  'ZV-LEGACY','confirmed','gazebo','Беседка','gazebo-1','Беседка №1',
  new Date('2030-10-18T10:00:00Z'),new Date('2030-10-18T13:00:00Z'),
  'Назаров Алексей Сергеевич','+79828387995',10,'544188872','544188872','VK bot','',
  new Date('2026-10-02T16:00:00Z'),new Date('2026-10-02T16:05:00Z'),'','544188872'
]);
const legacyServices = spreadsheet.insertSheet('Объекты и услуги');
legacyServices.appendRow(legacyServiceHeaders);
for (let i = 1; i <= 5; i++) {
  legacyServices.appendRow([true,'gazebo','Беседка','gazebo-' + i,'Беседка №' + i,'hourly','08:00','23:00',180,900,20,true,'legacy']);
}
legacyServices.appendRow([false,'corpus','Аренда корпуса','corpus-1','Корпус','date_range','','',0,0,0,true,'legacy']);
const legacyBlocks = spreadsheet.insertSheet('Блокировки');
legacyBlocks.appendRow(legacyBlockHeaders);
const legacySettings = spreadsheet.insertSheet('Настройки');
legacySettings.appendRow(legacySettingsHeaders);
legacySettings.appendRow(['timezone','Europe/Samara','Часовой пояс лагеря']);
legacySettings.appendRow(['hold_minutes','15','hold']);
legacySettings.appendRow(['version','5.9.1','old schema']);

context.setupSpreadsheet();
assert(legacyBookings.getLastRow() === 2, 'legacy migration changed booking row count');
const migratedBookingHeaders = legacyBookings.getRange(1,1,1,21).getValues()[0];
assert(migratedBookingHeaders[0] === 'booking_id' && migratedBookingHeaders[19] === 'price_amount' && migratedBookingHeaders[20] === 'price_text', 'legacy booking headers were not extended safely');
const legacyPreserved = post('get_booking', {booking_id:'ZV-LEGACY'});
assert(legacyPreserved.result.found && legacyPreserved.result.booking.status === 'confirmed', 'legacy confirmed booking was not preserved');
assert(legacyServices.getLastRow() === 7, 'legacy service rows were deleted/added');
const migratedGazebo = post('get_service', {service_key:'gazebo'});
assert(migratedGazebo.result.enabled && migratedGazebo.result.price_amount === 3300, 'legacy gazebo rows did not receive price fields');
const migratedCorpusRow = legacyServices.getRange(7,1,1,18).getValues()[0];
assert(migratedCorpusRow[0] === false && migratedCorpusRow[2] === 'Тур выходного дня' && migratedCorpusRow[14] === 1450, 'legacy corpus row migration failed');
const migratedManager = spreadsheet.getSheetByName('Бронирования — менеджер');
assert(migratedManager && migratedManager.getLastRow() === 2, 'manager sheet was not built from legacy bookings');
const migratedManagerRow = migratedManager.getRange(2,1,1,20).getValues()[0];
assert(migratedManagerRow[0] === 'Подтверждено' && migratedManagerRow[5] === 'Назаров Алексей Сергеевич', 'legacy booking was not represented correctly in manager sheet');

// Exact current production pool smoke: five independently bookable gazebos must occupy
// gazebo-1..gazebo-5 for the same interval; a sixth simultaneous hold must fail closed.
const poolResources = [];
for (let i = 1; i <= 5; i++) {
  const hold = post('create_hold', {
    service_key:'gazebo', start_at:'2035-10-18T10:00:00Z', end_at:'2035-10-18T13:00:00Z',
    guest_count:10, vk_user_id:700 + i, vk_peer_id:700 + i, ttl_minutes:15, request_id:'REQ-FIVE-' + i
  });
  assert(hold.ok && hold.result.available, 'one of five production gazebos was not bookable');
  poolResources.push(hold.result.resource_key);
}
assert(new Set(poolResources).size === 5, 'five simultaneous holds did not use five distinct gazebos');
assert(poolResources.includes('gazebo-1') && poolResources.includes('gazebo-5'), 'production gazebo pool keys are incomplete');
const sixthPoolHold = post('create_hold', {
  service_key:'gazebo', start_at:'2035-10-18T10:00:00Z', end_at:'2035-10-18T13:00:00Z',
  guest_count:10, vk_user_id:799, vk_peer_id:799, ttl_minutes:15, request_id:'REQ-FIVE-6'
});
assert(!sixthPoolHold.result.available && sixthPoolHold.result.reason === 'occupied', 'sixth simultaneous gazebo hold must be unavailable');

console.log('GOOGLE APPS SCRIPT OFFLINE SELF-TEST: PASS');
