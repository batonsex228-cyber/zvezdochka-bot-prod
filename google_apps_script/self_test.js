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
  setNumberFormat() { return this; }
}

class Sheet {
  constructor(name) { this.name = name; this.data = []; this.maxRows = 1000; }
  getLastRow() {
    let last = 0;
    this.data.forEach((row, i) => { if ((row || []).some(v => v !== '' && v !== null && v !== undefined)) last = i + 1; });
    return last;
  }
  getMaxRows() { return this.maxRows; }
  getRange(row, col, numRows = 1, numCols = 1) { return new Range(this, row, col, numRows, numCols); }
  getDataRange() {
    const rows = Math.max(this.getLastRow(), 1);
    const cols = Math.max(1, ...this.data.map(r => (r || []).length));
    return new Range(this, 1, 1, rows, cols);
  }
  appendRow(row) { this.data.push([...row]); }
  setFrozenRows() { return this; }
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
});
assert(submit.result.submitted, 'submit failed');

const decision = post('manager_decision', {booking_id: hold3.result.booking_id, decision: 'confirmed', manager_id: 9});
assert(decision.result.updated && decision.result.status === 'confirmed', 'manager confirmation failed');

console.log('GOOGLE APPS SCRIPT OFFLINE SELF-TEST: PASS');
