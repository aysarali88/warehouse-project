const WH_API_URL = 'https://warehouse-project-259.onrender.com/api/warehouse/rollout-daily-progress/sync';
const WH_SYNC_TOKEN = '';
const WH_SHEET_NAME = 'Daily Progress';

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('WH Sync')
    .addItem('Sync Rollout Daily Progress now', 'syncRolloutDailyProgressToWarehouse')
    .addItem('Install auto sync triggers', 'installWarehouseSyncTriggers')
    .addToUi();
}

function installWarehouseSyncTriggers() {
  ScriptApp.getProjectTriggers().forEach(trigger => {
    const fn = trigger.getHandlerFunction();
    if (fn === 'onEditWarehouseSync' || fn === 'syncRolloutDailyProgressToWarehouse') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
  ScriptApp.newTrigger('onEditWarehouseSync')
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onEdit()
    .create();
  ScriptApp.newTrigger('syncRolloutDailyProgressToWarehouse')
    .timeBased()
    .everyMinutes(5)
    .create();
  SpreadsheetApp.getActive().toast('WH auto sync triggers installed.', 'WH Sync', 5);
}

function onEditWarehouseSync(e) {
  const sheet = e && e.range ? e.range.getSheet() : null;
  if (!sheet || sheet.getName() !== WH_SHEET_NAME) return;
  syncRolloutDailyProgressToWarehouse();
}

function syncRolloutDailyProgressToWarehouse() {
  const spreadsheet = SpreadsheetApp.getActive();
  const sheet = spreadsheet.getSheetByName(WH_SHEET_NAME);
  if (!sheet) throw new Error(`Sheet not found: ${WH_SHEET_NAME}`);

  const lastRow = sheet.getLastRow();
  const lastCol = Math.min(sheet.getLastColumn(), 27);
  if (lastRow < 2 || lastCol < 1) {
    spreadsheet.toast('No Daily Progress rows to sync.', 'WH Sync', 5);
    return;
  }

  const values = sheet.getRange(1, 1, lastRow, lastCol).getDisplayValues();
  const headers = values[0].map(h => String(h || '').trim());
  const rows = values.slice(1)
    .map(valuesRow => rowToObject(headers, valuesRow))
    .filter(row => Object.values(row).some(value => String(value || '').trim()));

  const response = UrlFetchApp.fetch(WH_API_URL, {
    method: 'post',
    contentType: 'application/json',
    muteHttpExceptions: true,
    payload: JSON.stringify({
      token: WH_SYNC_TOKEN,
      source: `${spreadsheet.getName()} / ${WH_SHEET_NAME}`,
      rows,
    }),
  });

  const code = response.getResponseCode();
  const text = response.getContentText();
  if (code < 200 || code >= 300) {
    throw new Error(`WH sync failed (${code}): ${text}`);
  }
  const result = JSON.parse(text);
  spreadsheet.toast(`WH synced: ${result.created} new, ${result.updated} updated, total ${result.total}.`, 'WH Sync', 6);
}

function rowToObject(headers, valuesRow) {
  const row = {};
  headers.forEach((header, index) => {
    if (!header) return;
    row[header] = valuesRow[index] || '';
  });
  return row;
}
