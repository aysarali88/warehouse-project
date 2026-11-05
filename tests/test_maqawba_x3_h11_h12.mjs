import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const map = JSON.parse(readFileSync(new URL('../static/assets/fiber-map-data.json', import.meta.url), 'utf8'));
const html = readFileSync(new URL('../static/materials_inventory.html', import.meta.url), 'utf8');

test('Maqawba X3 H11-H12 cascade is represented as structured map data', () => {
  const boxes = map.boxes.filter(row => row.Area === 'Maqawba' && row['Related to XBOX'] === 'X3' && ['H11', 'H12'].includes(row.Hub));
  const routes = map.routes.filter(row => row.Area === 'Maqawba' && row['Related to XBOX'] === 'X3' && ['X3-H11', 'H11-H12'].includes(row['Route code']));

  assert.equal(boxes.length, 32);
  for (const hub of ['H11', 'H12']) {
    const hubBoxes = boxes.filter(row => row.Hub === hub);
    assert.equal(hubBoxes.length, 16);
    assert.ok(hubBoxes.every(row => row.Part === 'Part01'));
    for (let line = 1; line <= 4; line++) {
      const lineBoxes = hubBoxes.filter(row => row.Line === line).sort((a, b) => a.Splitter - b.Splitter);
      assert.deepEqual(lineBoxes.map(row => row['Box code']), [1, 2, 3, 4].map(splitter => `${hub}-L${line}-S${splitter}`));
      assert.deepEqual(lineBoxes.map(row => row['Box type']), ['SUB BOX', 'SUB BOX', 'SUB BOX', 'END BOX']);
    }
  }

  assert.equal(boxes.reduce((sum, row) => sum + row['Real length m'], 0), 1706);
  assert.equal(boxes.reduce((sum, row) => sum + row['Cable length m'], 0), 2500);
  assert.deepEqual(routes.map(row => [row['Route code'], row['Real length m'], row['Cable length m'], row['Material type']]), [
    ['X3-H11', 433, 500, '4-coreCable_500m'],
    ['H11-H12', 110, 150, '4-coreCable_150m'],
  ]);
  assert.ok(routes.every(row => row.Part === 'Part01'));
  assert.equal(new Set(boxes.map(row => row['Box code'])).size, 32);
});

test('Maqawba dashboard preserves targets without freezing actual single-core count', () => {
  const line = html.split('\n').find(row => row.includes("area:'Maqawba'"));
  assert.ok(line);
  for (const expected of [
    'targetSubEndBox:440', 'targetHubBox:32', 'targetXbox:3', 'targetCableMeters:40915', 'targetUsers:3456',
    'dashboardCableTargetOverride:40915', 'dashboardSubEndTargetOverride:440', 'dashboardHubTargetOverride:32',
    'dashboardXboxTargetOverride:3', 'dashboardSingleCoreTargetOverride:440', 'dashboardUsersTargetOverride:3456',
    'dashboardCableMetersOverride:37765', 'dashboardSubEndOverride:408',
  ]) assert.ok(line.includes(expected), `missing ${expected}`);
  assert.ok(!line.includes('dashboardSingleCoreOverride:'), 'actual single-core count should be calculated from rollout records');
});
