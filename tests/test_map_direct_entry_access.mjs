import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../static/materials_inventory.html', import.meta.url), 'utf8');
const helper = html.match(/function canUseMapDirectEntry\(\)\{[^}]+\}/)?.[0];

assert.ok(helper, 'map direct-entry access helper should be present');

function allowed({ username = '', role = 'Requester', program = 'FTTH' } = {}) {
  return vm.runInNewContext(`${helper}\ncanUseMapDirectEntry()`, {
    currentProgram: program,
    session: { username },
    isAdmin: () => role.toLowerCase() === 'admin',
    roleKey: () => role.toLowerCase(),
    normalizeProgram: value => String(value || '').toUpperCase(),
    normalizeText: value => String(value || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ''),
  });
}

test('map direct entry remains available to admins', () => {
  assert.equal(allowed({ username: 'OtherUser', role: 'Admin' }), true);
});

test('Ghassan can use direct map entry as an FTTH requester', () => {
  assert.equal(allowed({ username: 'Ghassan', role: 'Requester', program: 'FTTH' }), true);
});

test('direct map entry is available to FTTH requesters but not other roles or programs', () => {
  assert.equal(allowed({ username: 'OtherUser' }), true);
  assert.equal(allowed({ username: 'OtherUser', role: 'Warehouse Manager' }), false);
  assert.equal(allowed({ username: 'Ghassan', program: 'SINGLE_RAN' }), false);
});

test('map click, form opening, and save all use the scoped permission', () => {
  assert.match(html, /function bindMapEntryTargets\(root\)\{if\(!canUseMapDirectEntry\(\)\|\|!root\)/);
  assert.match(html, /function openMapFieldEntryFromTarget\(target\)\{\s*if\(!canUseMapDirectEntry\(\)\)/);
  assert.match(html, /async function saveMapFieldEntry\(confirmWarnings=false\)\{\s*if\(!canUseMapDirectEntry\(\)\|\|!mapFieldEntryState\.entry\)/);
});

test('map entry saves the selected cable length instead of the design default', () => {
  const payload = html.match(/function mapEntryPayload\(confirmWarnings=false\)\{[^\n]+\}/)?.[0];
  assert.ok(payload, 'map entry payload builder should be present');
  const result = vm.runInNewContext(`${payload}\nmapEntryPayload(true)`, {
    mapFieldEntryState: {
      submissionKey: 'field-entry-submission-key',
      entry: {
        item: 'Cable',
        codeType: 'cable',
        material: 'Single-Core Distribution Cable_50m',
        area: 'Al-Dhel Street',
        city: 'Tripoli',
        xbox: 'X4',
        code: 'H1-L1-S2',
      },
    },
    mapEntryValue: id => ({ mapEntryMaterial: 'Single-Core Distribution Cable_80m' })[id] || '',
  });

  assert.equal(result.material_type, 'Single-Core Distribution Cable_80m');
  assert.equal(result.confirm_warnings, true);
});

test('non-cable map entries keep their fixed material', () => {
  const payload = html.match(/function mapEntryPayload\(confirmWarnings=false\)\{[^\n]+\}/)?.[0];
  assert.ok(payload, 'map entry payload builder should be present');
  const result = vm.runInNewContext(`${payload}\nmapEntryPayload()`, {
    mapFieldEntryState: {
      submissionKey: 'field-entry-submission-key',
      entry: { item: 'SUB BOX', codeType: 'box', material: 'SUB BOX', area: 'Al-Dhel Street', city: 'Tripoli', xbox: 'X4', code: 'H1-L1-S2' },
    },
    mapEntryValue: () => 'should not replace box material',
  });

  assert.equal(result.material_type, 'SUB BOX');
});
