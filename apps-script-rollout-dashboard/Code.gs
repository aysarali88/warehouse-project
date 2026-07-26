// Rollout Daily Progress - Google Apps Script

var ROLLOUT_DAILY_PROGRESS_SS_ID = '1ZT9e9acJ9Y60J4f_DIFZiYyHa8GvNZdlTvpucHju7Ec';
var ROLLOUT_DAILY_PROGRESS_SS_URL = 'https://docs.google.com/spreadsheets/d/' + ROLLOUT_DAILY_PROGRESS_SS_ID + '/edit';
var ROLLOUT_DAILY_PROGRESS_SHEET_NAME = 'Daily Progress';
var ROLLOUT_AUDIT_SHEET_NAME = 'Audit_Log';
var ROLLOUT_LISTS_SHEET_NAME = 'Lists';
var ROLLOUT_USERS_SHEET_NAME = 'Users';
var ROLLOUT_CACHE_TTL_SECONDS = 90;
var ROLLOUT_RECORDS_CACHE_KEY = 'rollout_daily_progress_records_v1';
var ROLLOUT_LISTS_CACHE_KEY = 'rollout_daily_progress_lists_v1';
var ROLLOUT_MAX_RECORDS_RETURNED = 500;

var ROLLOUT_DEFAULT_USERS = [
  ['Management', 'Management', 'RDP-1020', 'Admin', 'Active'],
  ['ايسر عبيدات', 'ayser', 'RDP-1001', 'User', 'Active'],
  ['رياض كوامله', 'riyad', 'RDP-1002', 'User', 'Active'],
  ['حمزه بشايره', 'hamza', 'RDP-1003', 'User', 'Active'],
  ['محمد عادل', 'mohammad.adel', 'RDP-1004', 'User', 'Active'],
  ['غسان', 'ghassan', 'RDP-1005', 'User', 'Active'],
  ['نذير عصمان', 'natheer.osman', 'RDP-1006', 'User', 'Active'],
  ['عبدالرحمن ابو سليله', 'abdulrahman', 'RDP-1007', 'User', 'Active'],
  ['علي قراب', 'ali.qrab', 'RDP-1008', 'User', 'Active'],
  ['محمد ابو دبوس', 'mohammad.abudabos', 'RDP-1009', 'User', 'Active'],
  ['احمد ابو شرود', 'ahmad.abushrood', 'RDP-1010', 'User', 'Active'],
  ['عبد الباري', 'abdulbari', 'RDP-1011', 'User', 'Active']
];

var ROLLOUT_DAILY_PROGRESS_HEADERS = [
  'ID',
  'Date',
  'Supervisor Name',
  'team leader',
  'Area',
  'city',
  'Activity',
  'item',
  'material type',
  'mount type',
  'item serial',
  'actual',
  'stock remaining',
  'staus',
  'laser',
  'acceptance',
  'scan',
  'labeling',
  'entry time',
  'cable code',
  'box code',
  'OLT',
  'Cable route'
];

function doGet() {
  return HtmlService.createHtmlOutputFromFile('index')
    .setTitle('Rollout Daily Progress')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function authorizeRolloutAccess() {
  var sheet = getRolloutDailyProgressSheet_();
  getRolloutUsersSheet_();
  return {
    success: true,
    message: 'Rollout sheet access authorized',
    spreadsheet: sheet.getParent().getName(),
    sheet: sheet.getName(),
    rows: Math.max(sheet.getLastRow() - 1, 0)
  };
}

function testSpreadsheetPermission() {
  var ss = SpreadsheetApp.openById(ROLLOUT_DAILY_PROGRESS_SS_ID);
  return ss.getName();
}

function normalizeRolloutHeader_(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function getRolloutUsersSheet_() {
  var ss = getRolloutSpreadsheet_();
  var sheet = ss.getSheetByName(ROLLOUT_USERS_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(ROLLOUT_USERS_SHEET_NAME);
  }

  var headers = ['Name', 'Username', 'Password', 'Role', 'Status'];
  var headerRange = sheet.getRange(1, 1, 1, headers.length);
  var currentHeaders = headerRange.getValues()[0];
  var needsHeader = currentHeaders.join('') === '';
  for (var i = 0; i < headers.length; i++) {
    if (currentHeaders[i] !== headers[i]) {
      needsHeader = true;
      break;
    }
  }
  if (needsHeader) {
    headerRange.setValues([headers]);
    sheet.setFrozenRows(1);
  }

  var existing = {};
  if (sheet.getLastRow() >= 2) {
    var users = sheet.getRange(2, 1, sheet.getLastRow() - 1, headers.length).getValues();
    users.forEach(function(row, index) {
      var username = String(row[1] || '').trim().toLowerCase();
      if (username) existing[username] = { row: index + 2, name: String(row[0] || '') };
    });
    ROLLOUT_DEFAULT_USERS.forEach(function(defaultUser) {
      var username = String(defaultUser[1] || '').trim().toLowerCase();
      if (existing[username] && existing[username].name !== defaultUser[0]) {
        sheet.getRange(existing[username].row, 1).setValue(defaultUser[0]);
      }
    });
  }
  var missing = ROLLOUT_DEFAULT_USERS.filter(function(row) {
    return !existing[String(row[1] || '').trim().toLowerCase()];
  });
  if (missing.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, missing.length, headers.length).setValues(missing);
  }
  return sheet;
}

function loginRolloutUser(credentials) {
  try {
    var username = String(credentials && credentials.username || '').trim().toLowerCase();
    var password = String(credentials && credentials.password || '').trim();
    if (!username || !password) {
      return { success: false, message: 'Enter username and password' };
    }

    var sheet = getRolloutUsersSheet_();
    if (sheet.getLastRow() < 2) {
      return { success: false, message: 'No users found' };
    }
    var rows = sheet.getRange(2, 1, sheet.getLastRow() - 1, 5).getValues();
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var rowUsername = String(row[1] || '').trim().toLowerCase();
      var rowPassword = String(row[2] || '').trim();
      var status = String(row[4] || 'Active').trim().toLowerCase();
      if (rowUsername === username && rowPassword === password && status !== 'disabled') {
        return {
          success: true,
          user: {
            name: String(row[0] || ''),
            username: String(row[1] || ''),
            role: String(row[3] || 'User')
          }
        };
      }
    }
    return { success: false, message: 'Wrong username or password' };
  } catch (error) {
    return { success: false, message: 'Login failed: ' + error.toString() };
  }
}

function getRolloutSpreadsheet_() {
  try {
    return SpreadsheetApp.openById(ROLLOUT_DAILY_PROGRESS_SS_ID);
  } catch (error) {
    throw new Error('Cannot open Rollout Daily Progress sheet. Check access to: ' + ROLLOUT_DAILY_PROGRESS_SS_URL + '. ' + error.toString());
  }
}

function getRolloutDailyProgressSheet_() {
  var ss = getRolloutSpreadsheet_();
  var sheet = ss.getSheetByName(ROLLOUT_DAILY_PROGRESS_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(ROLLOUT_DAILY_PROGRESS_SHEET_NAME);
  }

  var headerRange = sheet.getRange(1, 1, 1, ROLLOUT_DAILY_PROGRESS_HEADERS.length);
  var currentHeaders = headerRange.getValues()[0];
  var needsHeader = currentHeaders.join('') === '';

  for (var i = 0; i < ROLLOUT_DAILY_PROGRESS_HEADERS.length; i++) {
    if (currentHeaders[i] !== ROLLOUT_DAILY_PROGRESS_HEADERS[i]) {
      needsHeader = true;
      break;
    }
  }

  if (needsHeader) {
    headerRange.setValues([ROLLOUT_DAILY_PROGRESS_HEADERS]);
    sheet.setFrozenRows(1);
  }

  return sheet;
}

function makeRolloutDailyProgressId_(sheet) {
  var maxId = 0;
  if (sheet.getLastRow() >= 2) {
    var ids = sheet.getRange(2, 1, sheet.getLastRow() - 1, 1).getValues();
    ids.forEach(function(row) {
      var match = String(row[0] || '').match(/(\d+)$/);
      if (match) maxId = Math.max(maxId, Number(match[1]));
    });
  }
  return 'RDP-' + String(maxId + 1).padStart(3, '0');
}

function normalizeRolloutDailyProgressRecord_(data, sheet) {
  var record = {};
  ROLLOUT_DAILY_PROGRESS_HEADERS.forEach(function(header) {
    record[header] = data && data[header] != null ? data[header] : '';
  });

  if (!record.ID) record.ID = makeRolloutDailyProgressId_(sheet);
  record.actual = Number(record.actual || 0);
  record['stock remaining'] = Number(record['stock remaining'] || 0);

  return record;
}

function findRolloutDailyProgressRowById_(sheet, id) {
  if (!id || sheet.getLastRow() < 2) return -1;
  var finder = sheet
    .getRange(2, 1, sheet.getLastRow() - 1, 1)
    .createTextFinder(String(id))
    .matchEntireCell(true);
  var cell = finder.findNext();
  return cell ? cell.getRow() : -1;
}

function rolloutDailyProgressRecordToRow_(record) {
  return ROLLOUT_DAILY_PROGRESS_HEADERS.map(function(header) {
    return record[header] != null ? record[header] : '';
  });
}

function getRolloutListSectionValues_(sheet, headerName) {
  var lastRow = sheet.getLastRow();
  var lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) return [];

  var values = sheet.getRange(1, 1, lastRow, lastColumn).getDisplayValues();
  var headers = values[0].map(function(value) { return String(value || '').trim(); });
  var target = normalizeRolloutHeader_(headerName);
  var start = -1;
  for (var i = 0; i < headers.length; i++) {
    if (normalizeRolloutHeader_(headers[i]) === target) {
      start = i;
      break;
    }
  }
  if (start < 0) return [];

  var end = start + 1;
  while (end < headers.length && !String(headers[end] || '').trim()) {
    end++;
  }

  var out = [];
  var seen = {};
  for (var row = 1; row < values.length; row++) {
    for (var col = start; col < end; col++) {
      var text = String(values[row][col] || '').trim();
      if (text && !seen[text]) {
        seen[text] = true;
        out.push(text);
      }
    }
  }
  return out;
}

function getRolloutLists_() {
  try {
    var cache = CacheService.getScriptCache();
    var cached = cache.get(ROLLOUT_LISTS_CACHE_KEY);
    if (cached) return JSON.parse(cached);

    var ss = getRolloutSpreadsheet_();
    var sheet = ss.getSheetByName(ROLLOUT_LISTS_SHEET_NAME);
    if (!sheet) {
      return { cableCodes: [], boxCodes: [] };
    }
    var cableCodes = getRolloutListSectionValues_(sheet, 'Cable code');
    var boxCodes = getRolloutListSectionValues_(sheet, 'Box code');
    if (!boxCodes.length) boxCodes = cableCodes.slice();
    var lists = {
      cableCodes: cableCodes,
      boxCodes: boxCodes
    };
    cache.put(ROLLOUT_LISTS_CACHE_KEY, JSON.stringify(lists), ROLLOUT_CACHE_TTL_SECONDS * 5);
    return lists;
  } catch (error) {
    return { cableCodes: [], boxCodes: [], message: error.toString() };
  }
}

function clearRolloutCache_() {
  try {
    CacheService.getScriptCache().removeAll([
      ROLLOUT_RECORDS_CACHE_KEY,
      ROLLOUT_LISTS_CACHE_KEY
    ]);
  } catch (error) {
    console.error('Rollout cache clear error:', error);
  }
}

function saveRolloutDailyProgress(data) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(10000);
    var sheet = getRolloutDailyProgressSheet_();
    var record = normalizeRolloutDailyProgressRecord_(data, sheet);
    var row = rolloutDailyProgressRecordToRow_(record);
    var existingRow = findRolloutDailyProgressRowById_(sheet, record.ID);
    var targetRow = existingRow >= 2 ? existingRow : sheet.getLastRow() + 1;
    var action = existingRow >= 2 ? 'UPDATE_ROLLOUT_PROGRESS' : 'ADD_ROLLOUT_PROGRESS';

    sheet.getRange(targetRow, 1, 1, row.length).setValues([row]);

    appendRolloutAuditLog_({
      spreadsheet: sheet.getParent().getName(),
      sheet: ROLLOUT_DAILY_PROGRESS_SHEET_NAME,
      action: action,
      cell: 'A' + targetRow,
      row: targetRow,
      newValue: record.ID,
      note: 'Rollout Daily Progress web app'
    });
    clearRolloutCache_();

    return {
      success: true,
      message: 'Progress saved',
      record: record
    };
  } catch (error) {
    return {
      success: false,
      message: 'Save failed: ' + error.toString()
    };
  } finally {
    try {
      lock.releaseLock();
    } catch (releaseError) {}
  }
}

function listRolloutDailyProgress() {
  try {
    var cache = CacheService.getScriptCache();
    var cached = cache.get(ROLLOUT_RECORDS_CACHE_KEY);
    if (cached) return JSON.parse(cached);

    var sheet = getRolloutDailyProgressSheet_();
    var lastRow = sheet.getLastRow();
    if (lastRow < 2) {
      var emptyResult = {
        success: true,
        headers: ROLLOUT_DAILY_PROGRESS_HEADERS,
        records: [],
        lists: getRolloutLists_()
      };
      cache.put(ROLLOUT_RECORDS_CACHE_KEY, JSON.stringify(emptyResult), ROLLOUT_CACHE_TTL_SECONDS);
      return emptyResult;
    }

    var totalRecords = lastRow - 1;
    var rowsToRead = Math.min(totalRecords, ROLLOUT_MAX_RECORDS_RETURNED);
    var startRow = lastRow - rowsToRead + 1;
    var values = sheet.getRange(startRow, 1, rowsToRead, ROLLOUT_DAILY_PROGRESS_HEADERS.length).getValues();
    var records = values.map(function(row) {
      var record = {};
      ROLLOUT_DAILY_PROGRESS_HEADERS.forEach(function(header, index) {
        var value = row[index];
        if (value instanceof Date) {
          value = Utilities.formatDate(value, Session.getScriptTimeZone(), 'yyyy-MM-dd');
        }
        record[header] = value;
      });
      return record;
    }).reverse();

    var result = {
      success: true,
      headers: ROLLOUT_DAILY_PROGRESS_HEADERS,
      records: records,
      totalRecords: totalRecords,
      truncated: totalRecords > rowsToRead,
      lists: getRolloutLists_()
    };
    try {
      cache.put(ROLLOUT_RECORDS_CACHE_KEY, JSON.stringify(result), ROLLOUT_CACHE_TTL_SECONDS);
    } catch (cacheError) {
      console.error('Rollout records cache error:', cacheError);
    }
    return result;
  } catch (error) {
    return {
      success: false,
      message: 'Load failed: ' + error.toString(),
      headers: ROLLOUT_DAILY_PROGRESS_HEADERS,
      records: []
    };
  }
}

function getRolloutAuditSheet_() {
  var ss = getRolloutSpreadsheet_();
  var sheet = ss.getSheetByName(ROLLOUT_AUDIT_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(ROLLOUT_AUDIT_SHEET_NAME);
    sheet.appendRow([
      'Timestamp',
      'User',
      'Spreadsheet',
      'Sheet',
      'Action',
      'Cell',
      'Row',
      'New Value',
      'Note'
    ]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function getRolloutAuditUser_() {
  try {
    var activeEmail = Session.getActiveUser().getEmail();
    if (activeEmail) return activeEmail;
  } catch (error1) {}
  try {
    var effectiveEmail = Session.getEffectiveUser().getEmail();
    if (effectiveEmail) return effectiveEmail;
  } catch (error2) {}
  return 'Unknown User';
}

function appendRolloutAuditLog_(entry) {
  try {
    var sheet = getRolloutAuditSheet_();
    sheet.appendRow([
      new Date(),
      getRolloutAuditUser_(),
      entry.spreadsheet || '',
      entry.sheet || '',
      entry.action || '',
      entry.cell || '',
      entry.row || '',
      entry.newValue || '',
      entry.note || ''
    ]);
  } catch (error) {
    console.error('Rollout audit log error:', error);
  }
}
