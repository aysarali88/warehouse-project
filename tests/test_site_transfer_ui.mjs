import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import SiteTransferUI from '../static/site_transfer_ui.js';

const siteOptions = {
  sites: [{ id: 7, name: 'North Site' }, { id: 9, name: 'South Site' }],
  products: [
    { id: 31, sku: 'MAT-31', name: 'Fiber Cable', uom: 'M' },
    { id: 32, sku: 'MAT-32', name: 'Hub Box', uom: 'PCS' },
  ],
};

function responseForTransfer(transfer) {
  const productById = new Map(siteOptions.products.map(row => [row.id, row]));
  const receivedTotal = transfer.items.reduce((sum, item) => sum + item.received, 0);
  const sentTotal = transfer.items.reduce((sum, item) => sum + item.quantity, 0);
  return {
    id: transfer.id,
    from_site_id: transfer.from_site_id,
    from_site: 'North Site',
    to_site_id: transfer.to_site_id,
    to_site: 'South Site',
    status: transfer.status,
    created_by: 'Admin',
    created_at: '2026-11-07T08:00:00Z',
    items: transfer.items.map(item => {
      const product = productById.get(item.product_id);
      return {
        id: item.id,
        product_id: item.product_id,
        product: product.name,
        sku: product.sku,
        uom: product.uom,
        requested_qty: String(item.quantity),
        sent_qty: String(transfer.dispatched ? item.quantity : 0),
        received_qty: String(item.received),
        in_transit_qty: String(transfer.dispatched && transfer.status !== 'cancelled' ? item.quantity - item.received : 0),
      };
    }),
    received_total: receivedTotal,
    sent_total: sentTotal,
  };
}

function makeApi({ disabled = false, failCreate = false, delayCreate = 0 } = {}) {
  const calls = [];
  const transfer = { id: null, from_site_id: 7, to_site_id: 9, status: 'draft', dispatched: false, items: [] };
  let createCount = 0;

  async function api(url, options = {}) {
    const parsed = new URL(url, 'https://warehouse.test');
    const path = parsed.pathname;
    const method = options.method || 'GET';
    calls.push({ path, method, query: parsed.searchParams, body: options.body ? JSON.parse(options.body) : null });

    if (path === '/api/warehouse/site-transfers' && method === 'GET') {
      if (disabled) throw new Error('Site-to-Site transfers are disabled until Site Inventory cutover is explicitly enabled for this program');
      return { items: transfer.id ? [responseForTransfer(transfer)] : [], total: transfer.id ? 1 : 0, page: 1, page_size: 25, total_pages: transfer.id ? 1 : 0 };
    }
    if (path === '/api/warehouse/site-inventory/options') return siteOptions;
    if (path === '/api/warehouse/site-inventory') {
      const productId = Number(parsed.searchParams.get('product_id'));
      const available = productId === 31 ? '100' : productId === 32 ? '30' : '0';
      return { items: [{ site_id: 7, product_id: productId, uom: productId === 31 ? 'M' : 'PCS', on_hand_qty: available }] };
    }
    if (path === '/api/warehouse/site-transfers' && method === 'POST') {
      createCount += 1;
      if (delayCreate) await new Promise(resolve => setTimeout(resolve, delayCreate));
      if (failCreate) throw new Error('Site balance changed; available quantity is insufficient');
      const body = calls.at(-1).body;
      transfer.id = 41;
      transfer.from_site_id = body.from_site_id;
      transfer.to_site_id = body.to_site_id;
      transfer.status = 'draft';
      transfer.dispatched = false;
      transfer.items = body.items.map((item, index) => ({
        id: 51 + index,
        product_id: item.product_id,
        quantity: Number(item.quantity),
        received: 0,
      }));
      return { transfer: responseForTransfer(transfer) };
    }
    const action = path.match(/^\/api\/warehouse\/site-transfers\/41\/(approve|dispatch|receipts)$/)?.[1];
    if (action === 'approve') transfer.status = 'approved';
    if (action === 'dispatch') {
      transfer.status = 'in_transit';
      transfer.dispatched = true;
    }
    if (action === 'receipts') {
      for (const receipt of calls.at(-1).body.items) {
        const item = transfer.items.find(row => row.id === receipt.transfer_item_id);
        item.received += Number(receipt.quantity);
      }
      const received = transfer.items.reduce((sum, item) => sum + item.received, 0);
      const sent = transfer.items.reduce((sum, item) => sum + item.quantity, 0);
      transfer.status = received >= sent ? 'received' : 'partially_received';
    }
    if (action) return { transfer: responseForTransfer(transfer) };
    throw new Error(`Unexpected API call: ${method} ${path}`);
  }

  return { api, calls, transfer, get createCount() { return createCount; } };
}

async function readyController(api, { isAdmin = () => true } = {}) {
  const controller = SiteTransferUI.createController({
    api,
    getProgram: () => 'FTTH',
    isAdmin,
    onChange() {},
  });
  await controller.open();
  await controller.updateForm('fromSiteId', '7');
  await controller.updateForm('toSiteId', '9');
  await controller.updateLine(controller.state.form.lines[0].id, 'productId', '31');
  return controller;
}

test('transfer validation rejects same site, excess quantity, and unknown free-text identities', () => {
  const base = { fromSiteId: '7', toSiteId: '9', productId: '31', uom: 'M', productUom: 'M', quantity: '20' };
  assert.match(SiteTransferUI.validateTransferForm({ ...base, toSiteId: '7' }, '100'), /different/);
  assert.match(SiteTransferUI.validateTransferForm({ ...base, quantity: '101' }, '100'), /available/);
  assert.match(SiteTransferUI.validateTransferForm({ ...base, productId: '', uom: '' }, '100'), /Select a material/);
  assert.equal(SiteTransferUI.validateTransferForm(base, '100'), '');
  assert.match(SiteTransferUI.validateReceiptQuantity('11', '10'), /cannot exceed/);
  assert.equal(SiteTransferUI.validateReceiptQuantity('5', '10'), '');
});

test('create, approve, dispatch, and repeated partial receipts follow backend status transitions', async () => {
  const mock = makeApi();
  const controller = await readyController(mock.api);
  const line = controller.state.form.lines[0];
  assert.equal(line.availableQty, '100');
  await controller.updateLine(line.id, 'quantity', '30');
  assert.equal(await controller.createTransfer({ reference: 'REQ-1', notes: 'Stage 1' }), true);
  assert.equal(controller.state.message, 'Transfer #41 created as Draft.');
  assert.ok(await controller.approve(41));
  assert.equal(controller.state.transfers[0].status, 'approved');
  assert.ok(await controller.dispatch(41));
  assert.equal(controller.state.transfers[0].status, 'in_transit');

  for (const [quantity, expectedStatus, expectedRemaining] of [
    ['5', 'partially_received', '25'],
    ['10', 'partially_received', '15'],
    ['15', 'received', '0'],
  ]) {
    const row = controller.state.transfers[0];
    assert.equal(controller.openReceipt(41, 51), true);
    await controller.receive({ ...controller.state.receipt, quantity, reference: 'RCV-1', notes: 'Received at site' });
    assert.equal(controller.state.transfers[0].status, expectedStatus);
    assert.equal(controller.state.transfers[0].items[0].in_transit_qty, expectedRemaining);
    assert.equal(row.id, 41);
  }

  const balanceCall = mock.calls.find(call => call.path === '/api/warehouse/site-inventory');
  assert.equal(balanceCall.query.get('site_id'), '7');
  assert.equal(balanceCall.query.get('product_id'), '31');
  assert.equal(mock.calls.some(call => call.path.includes('/ledger')), false);
  const create = mock.calls.find(call => call.method === 'POST' && call.path === '/api/warehouse/site-transfers');
  assert.deepEqual([create.body.from_site_id, create.body.to_site_id, create.body.items[0].product_id, create.body.items[0].uom], [7, 9, 31, 'M']);
});

test('one transfer can contain multiple materials with independent UOM and available quantities', async () => {
  const mock = makeApi();
  const controller = await readyController(mock.api);
  const first = controller.state.form.lines[0];
  await controller.updateLine(first.id, 'quantity', '40');
  assert.equal(controller.addLine(), true);
  const second = controller.state.form.lines[1];
  await controller.updateLine(second.id, 'productId', '32');
  await controller.updateLine(second.id, 'quantity', '20');
  assert.equal(second.availableQty, '30');

  assert.equal(await controller.createTransfer({ reference: 'MIXED-1' }), true);
  const create = mock.calls.find(call => call.method === 'POST' && call.path === '/api/warehouse/site-transfers');
  assert.deepEqual(create.body.items, [
    { product_id: 31, quantity: '40', uom: 'M' },
    { product_id: 32, quantity: '20', uom: 'PCS' },
  ]);
  const balanceQueries = mock.calls.filter(call => call.path === '/api/warehouse/site-inventory');
  assert.deepEqual(balanceQueries.map(call => call.query.get('product_id')), ['31', '32']);
  assert.equal(controller.state.transfers[0].items.length, 2);
});

test('each material is checked against its own available quantity before creating a transfer', async () => {
  const mock = makeApi();
  const controller = await readyController(mock.api);
  const first = controller.state.form.lines[0];
  await controller.updateLine(first.id, 'quantity', '10');
  controller.addLine();
  const second = controller.state.form.lines[1];
  await controller.updateLine(second.id, 'productId', '32');
  await controller.updateLine(second.id, 'quantity', '30.000001');

  assert.equal(await controller.createTransfer(), false);
  assert.match(controller.state.error, /Hub Box.*available Site Inventory balance/);
  assert.equal(mock.createCount, 0);
});

test('a product cannot be repeated in one transfer and lines can be added or removed', async () => {
  const mock = makeApi();
  const controller = await readyController(mock.api);
  const first = controller.state.form.lines[0];
  await controller.updateLine(first.id, 'quantity', '1');
  controller.addLine();
  const second = controller.state.form.lines[1];
  await controller.updateLine(second.id, 'productId', '31');
  await controller.updateLine(second.id, 'quantity', '2');
  assert.equal(await controller.createTransfer(), false);
  assert.match(controller.state.error, /only once/);
  assert.equal(mock.createCount, 0);
  assert.equal(controller.removeLine(second.id), true);
  assert.equal(controller.state.form.lines.length, 1);
  assert.equal(controller.removeLine(first.id), false);
});

test('feature-disabled response leaves the UI gated and sends no write requests', async () => {
  const mock = makeApi({ disabled: true });
  const controller = SiteTransferUI.createController({ api: mock.api, getProgram: () => 'FTTH', isAdmin: () => true, onChange() {} });
  assert.equal(await controller.open(), false);
  assert.equal(controller.state.gate, 'disabled');
  assert.match(controller.state.gateMessage, /disabled until Site Inventory cutover/);
  assert.equal(mock.calls.some(call => call.method !== 'GET'), false);
  assert.equal(mock.calls.some(call => call.path.endsWith('/options')), false);
});

test('page keeps transfer controls hidden until readiness and restricts navigation to Admin', async () => {
  const html = await readFile(new URL('../static/materials_inventory.html', import.meta.url), 'utf8');
  assert.match(html, /<button class="admin-only" data-view="siteTransfers">Site to Site Transfer<\/button>/);
  assert.match(html, /<section class="view admin-only site-transfer-view" id="siteTransfers">/);
  assert.match(html, /<div class="hidden" id="siteTransferWorkspace">/);
  assert.match(html, /id="siteTransferLines" class="site-transfer-lines"/);
  assert.match(html, /data-site-transfer-command="add-line">Add material/);
  assert.match(html, /<script src="\/static\/site_transfer_ui\.js"><\/script>/);
  assert.match(html, /href="\/static\/site_transfer_ui\.css"/);
  assert.equal(SiteTransferUI.PAGE_SIZE, 25);
});

test('same-site and excessive transfer quantities are rejected before create request', async () => {
  const mock = makeApi();
  const controller = await readyController(mock.api);
  const line = controller.state.form.lines[0];
  await controller.updateLine(line.id, 'quantity', '10');
  assert.equal(await controller.createTransfer({ toSiteId: '7' }), false);
  assert.match(controller.state.error, /different/);
  await controller.updateLine(line.id, 'quantity', '100.000001');
  assert.equal(await controller.createTransfer(), false);
  assert.match(controller.state.error, /available/);
  assert.equal(mock.createCount, 0);
});

test('transfer history requests server-side pages and applies status filters in the request', async () => {
  const mock = makeApi();
  const api = async (url, options) => {
    const data = await mock.api(url, options);
    const parsed = new URL(url, 'https://warehouse.test');
    if (parsed.pathname === '/api/warehouse/site-transfers' && !options?.method) {
      data.page = Number(parsed.searchParams.get('page'));
      data.total = 61;
      data.total_pages = 3;
    }
    return data;
  };
  const controller = await readyController(api);
  await controller.setPage(2);
  assert.equal(controller.state.page, 2);
  await controller.setStatus('received');
  const filtered = mock.calls.filter(call => call.path === '/api/warehouse/site-transfers' && call.method === 'GET').at(-1);
  assert.equal(filtered.query.get('page'), '1');
  assert.equal(filtered.query.get('page_size'), '25');
  assert.equal(filtered.query.get('status'), 'received');
});

test('concurrent submit calls issue one create request and backend errors are shown', async () => {
  const mock = makeApi({ delayCreate: 20 });
  const controller = await readyController(mock.api);
  await controller.updateLine(controller.state.form.lines[0].id, 'quantity', '10');
  const results = await Promise.all([controller.createTransfer(), controller.createTransfer()]);
  assert.deepEqual(results, [true, false]);
  assert.equal(mock.createCount, 1);

  const failed = makeApi({ failCreate: true });
  const failedController = await readyController(failed.api);
  await failedController.updateLine(failedController.state.form.lines[0].id, 'quantity', '10');
  assert.equal(await failedController.createTransfer(), false);
  assert.match(failedController.state.error, /available quantity is insufficient/);
});

test('non-admin cannot open transfer controls or call backend', async () => {
  const mock = makeApi();
  const controller = SiteTransferUI.createController({ api: mock.api, getProgram: () => 'FTTH', isAdmin: () => false, onChange() {} });
  assert.equal(await controller.open(), false);
  assert.equal(controller.state.gate, 'denied');
  assert.equal(mock.calls.length, 0);
});
