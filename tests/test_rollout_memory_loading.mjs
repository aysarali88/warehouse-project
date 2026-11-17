import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const html = await readFile(new URL('../static/materials_inventory.html', import.meta.url), 'utf8');

test('all inline materials-inventory scripts parse', () => {
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1])
    .filter(source => source.trim());
  assert.ok(scripts.length > 0);
  scripts.forEach((source, index) => new vm.Script(source, { filename: `materials_inventory-inline-${index}.js` }));
});

function sourceFor(name, nextName) {
  const start = html.indexOf(`async function ${name}(`);
  const end = html.indexOf(`async function ${nextName}(`, start + 1);
  assert.notEqual(start, -1, `${name} exists`);
  assert.notEqual(end, -1, `${nextName} boundary exists`);
  return html.slice(start, end).trim();
}

test('map loader requests only the selected area and XBOX, paging that scope', async () => {
  const fetchSource = sourceFor('fetchRolloutMapProgress', 'ensureRolloutMapProgress');
  const calls = [];
  const context = vm.createContext({
    URLSearchParams,
    api: async path => {
      const url = new URL(path, 'https://warehouse.test');
      calls.push(url);
      const offset = Number(url.searchParams.get('offset'));
      const records = offset === 0
        ? Array.from({ length: 500 }, (_, index) => ({ ID: `RDP-${index}` }))
        : [{ ID: 'RDP-500' }];
      return { count: 501, records };
    },
  });

  vm.runInContext(fetchSource, context);
  const result = await vm.runInContext('fetchRolloutMapProgress("Maqawba", "X1")', context);

  assert.equal(result.records.length, 501);
  assert.deepEqual(calls.map(url => url.searchParams.get('offset')), ['0', '500']);
  for (const url of calls) {
    assert.equal(url.pathname, '/api/warehouse/rollout-daily-progress');
    assert.equal(url.searchParams.get('area'), 'Maqawba');
    assert.equal(url.searchParams.get('xbox'), 'X1');
    assert.equal(url.searchParams.get('map_only'), 'true');
    assert.equal(url.searchParams.get('limit'), '500');
  }
});

test('opening Rollout tabs does not trigger an unfiltered full-record fetch', () => {
  assert.doesNotMatch(html, /function fetchAllRolloutDailyProgress\(/);
  assert.doesNotMatch(html, /function ensureRolloutDailyDetails\(/);

  const tabSwitch = html.match(/function switchRolloutDailyTab\([^\n]+/);
  assert.ok(tabSwitch);
  assert.doesNotMatch(tabSwitch[0], /ensureRolloutDailyDetails/);
  assert.match(tabSwitch[0], /ensureRolloutReportSummary/);
  assert.match(tabSwitch[0], /ensureRolloutOperationsSummary/);
});

test('Operations view renders the compact operations projection', () => {
  const operationsStart = html.indexOf('function renderRolloutOperations(){');
  const operationsEnd = html.indexOf('\n}', operationsStart);
  assert.notEqual(operationsStart, -1);
  const operations = html.slice(operationsStart, operationsEnd);
  assert.match(operations, /wh\.rolloutOperationsProgress/);
  assert.doesNotMatch(operations, /wh\.rolloutDailyProgress/);
});

test('area report XBOX pivot is calculated from its compact report rows', () => {
  const start = html.indexOf('function rolloutPivotData(');
  const end = html.indexOf('\n}', start);
  assert.notEqual(start, -1);
  const pivot = html.slice(start, end);
  assert.match(pivot, /rolloutAreaInstalledMetrics\(area,source\)\.xbox/);
});
