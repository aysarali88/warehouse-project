import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const html = await readFile(new URL('../static/materials_inventory.html', import.meta.url), 'utf8');

function sourceBetween(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  const end = html.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(start, -1, `start marker exists: ${startMarker}`);
  assert.notEqual(end, -1, `end marker exists: ${endMarker}`);
  return html.slice(start, end);
}

function mapLoaderContext(api) {
  const context = vm.createContext({
    api,
    applyFiberMapReference(data) { context.warehouseFiberReference = data; },
    document: { querySelector: () => null },
    renderRolloutDailyProgress() {},
    renderRolloutCityOverview() {},
    renderRolloutMapPanel() {},
    setTimeout(callback) { callback(); return 1; },
    warehouseFiberReference: null,
    warehouseFiberReferenceLoading: false,
    warehouseFiberReferencePromise: null,
    warehouseFiberReferenceError: '',
  });
  vm.runInContext(sourceBetween(
    'function ensureRolloutDashboardDesignReference(force=false){',
    '\nasync function checkFiberMapReferenceVersion()'
  ), context);
  return context;
}

test('map reference request is shared by concurrent callers', async () => {
  let calls = 0;
  let resolveRequest;
  const context = mapLoaderContext(() => {
    calls += 1;
    return new Promise(resolve => { resolveRequest = resolve; });
  });
  const first = vm.runInContext('ensureRolloutDashboardDesignReference()', context);
  const second = vm.runInContext('ensureRolloutDashboardDesignReference()', context);
  await Promise.resolve();
  assert.equal(calls, 1);
  resolveRequest({ boxes: [], routes: [], revision: 'r1' });
  await Promise.all([first, second]);
  assert.equal(context.warehouseFiberReference.revision, 'r1');
});

test('401, 403, and 500 map failures do not retry or recursively reload', async () => {
  for (const status of [401, 403, 500]) {
    let calls = 0;
    const context = mapLoaderContext(() => {
      calls += 1;
      const error = new Error(`HTTP ${status}`);
      error.status = status;
      throw error;
    });
    assert.equal(await vm.runInContext('ensureRolloutDashboardDesignReference()', context), null);
    assert.equal(calls, 1, `HTTP ${status} should make one request`);
    assert.equal(await vm.runInContext('ensureRolloutDashboardDesignReference()', context), null);
    assert.equal(calls, 1, `HTTP ${status} failure should remain stopped until manual retry`);
    assert.match(context.warehouseFiberReferenceError, new RegExp(String(status)));
  }
});

test('temporary network timeout retries once and then loads successfully', async () => {
  let calls = 0;
  const context = mapLoaderContext(() => {
    calls += 1;
    if (calls === 1) {
      const error = new Error('network timeout');
      error.name = 'AbortError';
      throw error;
    }
    return Promise.resolve({ boxes: [], routes: [], revision: 'r2' });
  });
  const result = await vm.runInContext('ensureRolloutDashboardDesignReference()', context);
  assert.equal(calls, 2);
  assert.equal(result.revision, 'r2');
  assert.equal(context.warehouseFiberReferenceError, '');
});

test('manual map retry succeeds after an earlier failure', async () => {
  let calls = 0;
  const context = mapLoaderContext(() => {
    calls += 1;
    if (calls === 1) {
      const error = new Error('HTTP 500');
      error.status = 500;
      throw error;
    }
    return Promise.resolve({ boxes: [], routes: [], revision: 'r3' });
  });
  await vm.runInContext('ensureRolloutDashboardDesignReference()', context);
  const result = await vm.runInContext('retryRolloutDashboardDesignReference()', context);
  assert.equal(calls, 2);
  assert.equal(result.revision, 'r3');
});

function authContext(fetchImpl, session = { id: 1 }) {
  const elements = new Map();
  const element = () => ({
    classList: { add() {}, remove() {} },
    isConnected: false,
    setAttribute() {},
    removeAttribute() {},
  });
  const context = vm.createContext({
    fetch: fetchImpl,
    document: { activeElement: null, getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); } },
    FormData,
    HTMLButtonElement: class HTMLButtonElement {},
    withProgramQuery: path => path,
    normalizeProgram: value => value || 'FTTH',
    scopedJsonBody: body => body,
    toast() {},
    session,
    csrfToken: 'csrf-before',
    reauthenticationRequired: false,
    activeUser: 'Requester',
    activeRole: 'Requester',
    activeWarehouseAssignment: 'Area A',
    currentProgram: 'FTTH',
    pendingMutationRequests: new Map(),
  });
  const authSource = sourceBetween(
    'function requireReauthentication(){',
    '\nasync function reconcileDamageInventory()'
  );
  vm.runInContext(authSource, context);
  return context;
}

function response(status, body = {}) {
  return { status, ok: status >= 200 && status < 300, text: async () => JSON.stringify(body), json: async () => body };
}

test('authenticated API handles 401 once and does not retry the failed request', async () => {
  let calls = 0;
  const context = authContext(async () => { calls += 1; return response(401, { detail: 'Unauthorized' }); });
  await assert.rejects(vm.runInContext("api('/api/warehouse/bootstrap')", context), error => error.status === 401);
  assert.equal(calls, 1);
  assert.equal(context.session, null);
  assert.equal(context.reauthenticationRequired, true);
  assert.equal(context.activeRole, '');
  await assert.rejects(vm.runInContext("api('/api/warehouse/notifications')", context), error => error.status === 401);
  assert.equal(calls, 1, 'requests after invalidation are stopped before fetch');
  vm.runInContext(sourceBetween('function canRunAuthenticatedPolls(){', '\nasync function sendPresenceHeartbeat()'), context);
  assert.equal(vm.runInContext('canRunAuthenticatedPolls()', context), false);
});

test('initial unauthenticated /auth/me response remains a normal visitor state', async () => {
  let calls = 0;
  const context = authContext(async () => { calls += 1; return response(401, { detail: 'Not logged in' }); }, null);
  await assert.rejects(vm.runInContext("api('/api/auth/me')", context), error => error.status === 401);
  assert.equal(calls, 1);
  assert.equal(context.reauthenticationRequired, false);
  assert.equal(context.session, null);
});

test('403 permissions failure and HTTP 500 do not invalidate the session', async () => {
  for (const status of [403, 500]) {
    let calls = 0;
    const context = authContext(async () => { calls += 1; return response(status, { detail: status === 403 ? 'Forbidden' : 'Server error' }); });
    await assert.rejects(vm.runInContext("api('/api/warehouse/bootstrap')", context), error => error.status === status);
    assert.equal(calls, 1);
    assert.equal(context.session.id, 1);
    assert.equal(context.reauthenticationRequired, false);
  }
});

test('invalid-CSRF 403 keeps its single token refresh path without treating it as expired auth', async () => {
  let calls = 0;
  const context = authContext(async path => {
    calls += 1;
    if (path === '/api/auth/me') return response(200, { user: { id: 1 }, csrf_token: 'csrf-after' });
    if (calls === 1) return response(403, { detail: 'Invalid CSRF token' });
    return response(200, { success: true });
  });
  const result = await vm.runInContext("api('/api/write', { method: 'POST', body: '{}' })", context);
  assert.equal(result.success, true);
  assert.equal(calls, 3);
  assert.equal(context.session.id, 1);
  assert.equal(context.reauthenticationRequired, false);
});

test('expired session disables authenticated polling and hidden tabs do not poll', () => {
  const pollingSource = sourceBetween('function canRunAuthenticatedPolls(){', '\nasync function sendPresenceHeartbeat()');
  const context = vm.createContext({
    session: { id: 1 },
    reauthenticationRequired: false,
    document: { visibilityState: 'visible' },
  });
  vm.runInContext(pollingSource, context);
  assert.equal(vm.runInContext('canRunAuthenticatedPolls()', context), true);
  context.document.visibilityState = 'hidden';
  assert.equal(vm.runInContext('canRunAuthenticatedPolls()', context), false);
  context.document.visibilityState = 'visible';
  context.session = null;
  assert.equal(vm.runInContext('canRunAuthenticatedPolls()', context), false);
  assert.match(html, /setInterval\(\(\)=>\{if\(canRunAuthenticatedPolls\(\)\)pollNotifications\(true\)\}/);
  assert.match(html, /setInterval\(\(\)=>\{if\(!canRunAuthenticatedPolls\(\)\)return;sendPresenceHeartbeat\(\)/);
  assert.match(html, /if\(!canRunAuthenticatedPolls\(\)\|\|!activeUser\|\|!csrfToken\)return/);
});

test('map field save uses authenticated fetch and concurrent clicks send only one POST', async () => {
  let calls = 0;
  let releaseRequest;
  let savedRecords = 0;
  const errors = [];
  const context = vm.createContext({
    canUseMapDirectEntry: () => true,
    csrfToken: 'csrf',
    mapFieldEntryState: { entry: { code: 'H1-L1-S1' }, saving: false, submissionKey: 'stable-key' },
    mapEntrySetSaving(value) { context.mapFieldEntryState.saving = value; },
    authenticatedFetch() {
      calls += 1;
      return new Promise(resolve => { releaseRequest = resolve; });
    },
    withProgramQuery: path => path,
    mapEntryPayload: () => ({ submission_key: context.mapFieldEntryState.submissionKey }),
    applyRolloutMutation() { savedRecords += 1; },
    closeMapFieldEntry() { context.mapFieldEntryState.entry = null; },
    renderRolloutMapPanel() {},
    renderRolloutDailyProgress() {},
    renderRolloutUsage() {},
    toast(message) { errors.push(message); },
  });
  vm.runInContext(sourceBetween('async function saveMapFieldEntry(confirmWarnings=false){', '\nfunction bindMapEntryTargets(root)'), context);
  const first = vm.runInContext('saveMapFieldEntry()', context);
  const second = vm.runInContext('saveMapFieldEntry()', context);
  assert.equal(calls, 1, errors.join('; '));
  releaseRequest({ ok: true, status: 200, json: async () => ({ record: { ID: 'R-1' } }) });
  await Promise.all([first, second]);
  assert.equal(savedRecords, 1);
  assert.equal(context.mapFieldEntryState.entry, null);
  assert.match(sourceBetween('async function saveMapFieldEntry(confirmWarnings=false){', '\nfunction bindMapEntryTargets(root)'), /authenticatedFetch\(/);
});

test('map field save does not automatically retry a failed write', async () => {
  let calls = 0;
  const errors = [];
  const context = vm.createContext({
    canUseMapDirectEntry: () => true,
    csrfToken: 'csrf',
    mapFieldEntryState: { entry: { code: 'H1-L1-S1' }, saving: false, submissionKey: 'stable-key' },
    mapEntrySetSaving(value) { context.mapFieldEntryState.saving = value; },
    authenticatedFetch: async () => { calls += 1; return { ok: false, status: 500, json: async () => ({ detail: 'Server error' }) }; },
    withProgramQuery: path => path,
    mapEntryPayload: () => ({ submission_key: context.mapFieldEntryState.submissionKey }),
    toast(message) { errors.push(message); },
  });
  vm.runInContext(sourceBetween('async function saveMapFieldEntry(confirmWarnings=false){', '\nfunction bindMapEntryTargets(root)'), context);
  await vm.runInContext('saveMapFieldEntry()', context);
  assert.equal(calls, 1, errors.join('; '));
  assert.equal(context.mapFieldEntryState.entry.code, 'H1-L1-S1');
});

test('reauthentication prompt preserves the app form DOM and current program', () => {
  assert.match(html, /id="sessionExpiredNotice"[^>]*role="alert"/);
  assert.match(html, /id="loginBackButton"/);
  const handler = sourceBetween('function requireReauthentication(){', '\nasync function authenticatedFetch');
  const nodes = new Map();
  const context = vm.createContext({
    session: { id: 7 }, csrfToken: 'token', reauthenticationRequired: false,
    activeUser: 'Requester', activeRole: 'Requester', activeWarehouseAssignment: 'Area A', currentProgram: 'FTTH',
    document: { getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, { value: id === 'mapEntryActual' ? '3' : '', classList: { add() {}, remove() {} } });
      return nodes.get(id);
    } },
  });
  nodes.set('mapEntryActual', { value: '3', classList: { add() {}, remove() {} } });
  vm.runInContext(handler, context);
  vm.runInContext('requireReauthentication()', context);
  assert.equal(context.session, null);
  assert.equal(context.reauthenticationRequired, true);
  assert.equal(context.currentProgram, 'FTTH');
  assert.equal(nodes.get('mapEntryActual').value, '3');
});
