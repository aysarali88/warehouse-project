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

test('direct map entry remains unavailable to other requesters and programs', () => {
  assert.equal(allowed({ username: 'OtherUser' }), false);
  assert.equal(allowed({ username: 'Ghassan', program: 'SINGLE_RAN' }), false);
  assert.equal(allowed({ username: 'not-ghassan' }), false);
});

test('map click, form opening, and save all use the scoped permission', () => {
  assert.match(html, /function bindMapEntryTargets\(root\)\{if\(!canUseMapDirectEntry\(\)\|\|!root\)/);
  assert.match(html, /function openMapFieldEntryFromTarget\(target\)\{\s*if\(!canUseMapDirectEntry\(\)\)/);
  assert.match(html, /async function saveMapFieldEntry\(confirmWarnings=false\)\{\s*if\(!canUseMapDirectEntry\(\)\|\|!mapFieldEntryState\.entry\)/);
});
