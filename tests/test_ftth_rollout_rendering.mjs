import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../static/ftth_rollout.html', import.meta.url), 'utf8');
const escSource = html.match(/function esc\(v\)\{[\s\S]*?(?=function toast\()/)?.[0];
const pillClassSource = html.split(/\r?\n/).find(line => line.startsWith('function pillClass('));
const tableSource = html.split(/\r?\n/).find(line => line.startsWith('function table('));

assert.ok(escSource, 'production HTML escaping helper should be present');
assert.ok(pillClassSource, 'production pill class mapper should be present');
assert.ok(tableSource, 'production table renderer should be present');

function render(columns, rows) {
  let rendered = '';
  const context = vm.createContext({
    document: { getElementById: () => ({ set innerHTML(value) { rendered = value; } }) },
  });
  vm.runInContext([escSource, pillClassSource, tableSource].join('\n'), context);
  vm.runInContext(`table('target', ${JSON.stringify(columns)}, ${JSON.stringify(Array.isArray(rows) ? rows : [rows])})`, context);
  return rendered;
}

test('normal rollout statuses keep their existing pill colors', () => {
  const output = render(
    ['staus', 'laser'],
    [
      { staus: 'Done', laser: 'Yes' },
      { staus: 'In Progress', laser: 'No' },
      { staus: 'Blocked' },
      { staus: 'Planned' },
    ],
  );

  for (const expected of [
    '<span class="pill done">Done</span>',
    '<span class="pill progress">In Progress</span>',
    '<span class="pill blocked">Blocked</span>',
    '<span class="pill planned">Planned</span>',
    '<span class="pill yes">Yes</span>',
    '<span class="pill no">No</span>',
  ]) {
    assert.ok(output.includes(expected), `expected rendered markup: ${expected}`);
  }
});

test('unrecognized status text cannot create HTML attributes or classes', () => {
  const payload = 'Blocked" onmouseover="alert(1)';
  const output = render(['staus'], [{ staus: payload }, { staus: 'constructor' }]);

  assert.match(output, /<span class="pill">Blocked&quot; onmouseover=&quot;alert\(1\)<\/span>/);
  assert.match(output, /<span class="pill">constructor<\/span>/);
  assert.doesNotMatch(output, /<span[^>]*\sonmouseover=/);
});
