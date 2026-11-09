(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SiteTransferUI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const SCALE = 1000000n;
  const PAGE_SIZE = 25;

  function quantityUnits(value) {
    let raw = String(value ?? '').trim();
    if (/^\.\d+$/.test(raw)) raw = `0${raw}`;
    if (!/^\d{1,12}(?:\.\d{0,6})?$/.test(raw)) return null;
    const [whole, fraction = ''] = raw.split('.');
    return BigInt(whole) * SCALE + BigInt(fraction.padEnd(6, '0'));
  }

  function validateReceiptQuantity(quantity, remaining) {
    const requested = quantityUnits(quantity);
    const available = quantityUnits(remaining);
    if (requested === null || requested <= 0n) return 'Enter a quantity greater than zero with at most 6 decimal places.';
    if (available === null || requested > available) return 'Received quantity cannot exceed the remaining in-transit quantity.';
    return '';
  }

  function validateTransferForm(form, availableQty) {
    if (!Number(form.fromSiteId) || !Number(form.toSiteId)) return 'Select both sites.';
    if (Number(form.fromSiteId) === Number(form.toSiteId)) return 'From Site and To Site must be different.';
    if (!Number(form.productId)) return 'Select a material.';
    if (!form.uom || form.uom !== form.productUom) return 'Material unit does not match the selected product.';
    const requested = quantityUnits(form.quantity);
    const available = quantityUnits(availableQty);
    if (requested === null || requested <= 0n) return 'Enter a quantity greater than zero with at most 6 decimal places.';
    if (available === null || requested > available) return 'Transfer quantity cannot exceed the available Site Inventory balance.';
    return '';
  }

  function errorMessage(error) {
    return String(error?.message || 'The request could not be completed.');
  }

  function isFeatureDisabled(error) {
    return /disabled until Site Inventory cutover is explicitly enabled/i.test(errorMessage(error));
  }

  function createController({ api, getProgram, isAdmin, onChange, confirm }) {
    const state = {
      gate: 'idle',
      gateMessage: '',
      error: '',
      message: '',
      sites: [],
      products: [],
      transfers: [],
      page: 1,
      pageSize: PAGE_SIZE,
      total: 0,
      totalPages: 0,
      status: '',
      loading: false,
      busy: false,
      availabilityLoading: false,
      availabilityLoaded: false,
      availabilityError: '',
      availableQty: '0',
      form: { fromSiteId: '', toSiteId: '', productId: '', uom: '', quantity: '', reference: '', notes: '' },
      receipt: null,
      program: '',
    };

    const publish = () => onChange(state);
    const program = () => String(getProgram() || 'FTTH').trim().toUpperCase();
    const url = (path, params = {}) => {
      const query = new URLSearchParams({ program: program(), ...params });
      return `${path}?${query.toString()}`;
    };
    const requestToken = () => {
      if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
      return `ui-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
    };
    const setError = (error) => {
      state.error = errorMessage(error);
      if (isFeatureDisabled(error)) {
        state.gate = 'disabled';
        state.gateMessage = state.error;
        state.transfers = [];
      }
      publish();
    };

    async function fetchList(page = state.page) {
      const params = { page: String(page), page_size: String(state.pageSize) };
      if (state.status) params.status = state.status;
      const data = await api(url('/api/warehouse/site-transfers', params));
      state.transfers = Array.isArray(data.items) ? data.items : [];
      state.page = Number(data.page || page);
      state.total = Number(data.total || 0);
      state.totalPages = Number(data.total_pages || 0);
      return data;
    }

    async function fetchOptions() {
      const data = await api(url('/api/warehouse/site-inventory/options'));
      state.sites = Array.isArray(data.sites) ? data.sites : [];
      state.products = Array.isArray(data.products) ? data.products : [];
    }

    async function open() {
      if (!isAdmin()) {
        state.gate = 'denied';
        state.gateMessage = 'Admin access is required.';
        publish();
        return false;
      }
      const nextProgram = program();
      const changedProgram = state.program !== nextProgram;
      if (changedProgram) {
        state.program = nextProgram;
        state.sites = [];
        state.products = [];
        state.transfers = [];
        state.form = { fromSiteId: '', toSiteId: '', productId: '', uom: '', quantity: '', reference: '', notes: '' };
        state.availabilityLoaded = false;
        state.availableQty = '0';
      }
      state.gate = 'checking';
      state.gateMessage = 'Checking transfer readiness…';
      state.error = '';
      state.message = '';
      state.loading = true;
      publish();
      try {
        await fetchList(1);
        state.gate = 'enabled';
        state.gateMessage = '';
        if (!state.sites.length || !state.products.length) await fetchOptions();
        state.loading = false;
        publish();
        if (state.form.fromSiteId && state.form.productId) await refreshAvailability();
        return true;
      } catch (error) {
        state.loading = false;
        if (isFeatureDisabled(error)) {
          state.gate = 'disabled';
          state.gateMessage = errorMessage(error);
          state.error = '';
          state.transfers = [];
        } else {
          state.gate = 'unavailable';
          state.gateMessage = errorMessage(error);
        }
        publish();
        return false;
      }
    }

    async function loadPage(page = state.page) {
      if (state.gate !== 'enabled' || state.busy) return false;
      state.loading = true;
      state.error = '';
      publish();
      try {
        await fetchList(page);
        state.loading = false;
        publish();
        return true;
      } catch (error) {
        state.loading = false;
        setError(error);
        return false;
      }
    }

    async function refreshAvailability() {
      const { fromSiteId, productId } = state.form;
      state.availabilityError = '';
      if (state.gate !== 'enabled' || !fromSiteId || !productId) {
        state.availableQty = '0';
        state.availabilityLoaded = false;
        publish();
        return false;
      }
      state.availabilityLoading = true;
      state.availabilityLoaded = false;
      publish();
      try {
        const data = await api(url('/api/warehouse/site-inventory', {
          site_id: String(fromSiteId), product_id: String(productId),
        }));
        const row = (data.items || []).find(item =>
          String(item.site_id) === String(fromSiteId) && String(item.product_id) === String(productId)
        );
        state.availableQty = row?.on_hand_qty ?? '0';
        state.availabilityLoaded = true;
        state.availabilityLoading = false;
        publish();
        return true;
      } catch (error) {
        state.availabilityLoading = false;
        state.availabilityError = errorMessage(error);
        state.availabilityLoaded = false;
        publish();
        return false;
      }
    }

    function updateForm(field, value) {
      if (!Object.prototype.hasOwnProperty.call(state.form, field)) return;
      state.form[field] = value;
      if (field === 'productId') {
        const product = state.products.find(row => String(row.id) === String(value));
        state.form.uom = product?.uom || '';
      }
      state.error = '';
      publish();
      if (field === 'fromSiteId' || field === 'productId') return refreshAvailability();
      return Promise.resolve(true);
    }

    async function refreshAfterAction() {
      try {
        await fetchList(state.page);
        state.error = '';
      } catch (error) {
        state.error = `Action succeeded, but the transfer list could not refresh: ${errorMessage(error)}`;
      }
      await refreshAvailability();
      publish();
    }

    async function createTransfer(values = {}) {
      if (state.busy || state.gate !== 'enabled' || !state.availabilityLoaded) return false;
      const form = { ...state.form, ...values };
      const product = state.products.find(row => String(row.id) === String(form.productId));
      const siteIds = new Set(state.sites.map(row => String(row.id)));
      const validation = validateTransferForm({
        fromSiteId: form.fromSiteId,
        toSiteId: form.toSiteId,
        productId: form.productId,
        uom: form.uom,
        productUom: product?.uom || '',
        quantity: form.quantity,
      }, state.availableQty);
      if (!siteIds.has(String(form.fromSiteId)) || !siteIds.has(String(form.toSiteId)) || !product) {
        state.error = 'Select sites and a product from the loaded options.';
        publish();
        return false;
      }
      if (validation) {
        state.error = validation;
        publish();
        return false;
      }
      state.busy = true;
      state.error = '';
      state.message = '';
      publish();
      try {
        const data = await api(url('/api/warehouse/site-transfers'), {
          method: 'POST',
          body: JSON.stringify({
            program: program(),
            from_site_id: Number(form.fromSiteId),
            to_site_id: Number(form.toSiteId),
            items: [{ product_id: Number(product.id), quantity: form.quantity, uom: product.uom }],
            reference: form.reference,
            notes: form.notes,
            request_token: requestToken(),
          }),
        });
        state.message = `Transfer #${data.transfer?.id ?? ''} created as Draft.`;
        state.form.quantity = '';
        state.form.reference = '';
        state.form.notes = '';
        await refreshAfterAction();
        return true;
      } catch (error) {
        setError(error);
        return false;
      } finally {
        state.busy = false;
        publish();
      }
    }

    async function performAction(transferId, action, body = {}) {
      if (state.busy || state.gate !== 'enabled') return false;
      state.busy = true;
      state.error = '';
      state.message = '';
      publish();
      try {
        const data = await api(url(`/api/warehouse/site-transfers/${Number(transferId)}/${action}`), {
          method: 'POST',
          body: JSON.stringify({ program: program(), ...body }),
        });
        state.message = action === 'approve'
          ? `Transfer #${transferId} approved.`
          : `Transfer #${transferId} updated.`;
        await refreshAfterAction();
        return data;
      } catch (error) {
        setError(error);
        return false;
      } finally {
        state.busy = false;
        publish();
      }
    }

    async function approve(transferId) {
      return performAction(transferId, 'approve');
    }

    async function dispatch(transferId) {
      return performAction(transferId, 'dispatch', { request_token: requestToken() });
    }

    function openReceipt(transferId, itemId) {
      if (state.busy || state.gate !== 'enabled') return false;
      const transfer = state.transfers.find(row => Number(row.id) === Number(transferId));
      const item = transfer?.items?.find(row => Number(row.id) === Number(itemId));
      if (!transfer || !item || !['in_transit', 'partially_received'].includes(transfer.status)) return false;
      state.receipt = {
        transferId: Number(transferId), itemId: Number(itemId),
        product: item.product, uom: item.uom,
        remaining: item.in_transit_qty, quantity: '', reference: '', notes: '',
      };
      state.error = '';
      publish();
      return true;
    }

    function closeReceipt() {
      state.receipt = null;
      state.error = '';
      publish();
    }

    async function receive(input = state.receipt) {
      if (state.busy || !input || state.gate !== 'enabled') return false;
      const validation = validateReceiptQuantity(input.quantity, input.remaining);
      if (validation) {
        state.error = validation;
        publish();
        return false;
      }
      state.busy = true;
      state.error = '';
      state.message = '';
      publish();
      try {
        await api(url(`/api/warehouse/site-transfers/${Number(input.transferId)}/receipts`), {
          method: 'POST',
          body: JSON.stringify({
            program: program(),
            request_token: requestToken(),
            items: [{ transfer_item_id: Number(input.itemId), quantity: input.quantity, uom: input.uom || input.unit || state.receipt?.uom }],
            reference: input.reference || '',
            notes: input.notes || '',
          }),
        });
        state.message = `Receipt recorded for transfer #${input.transferId}.`;
        state.receipt = null;
        await refreshAfterAction();
        return true;
      } catch (error) {
        setError(error);
        return false;
      } finally {
        state.busy = false;
        publish();
      }
    }

    async function reverseDispatch(transferId, { note, confirmPhysicalReturn } = {}) {
      if (state.busy || state.gate !== 'enabled') return false;
      if (!String(note || '').trim()) {
        state.error = 'A correction note is required.';
        publish();
        return false;
      }
      if (!confirmPhysicalReturn) {
        state.error = 'Confirm that the dispatched material is physically back at the From Site.';
        publish();
        return false;
      }
      if (typeof confirm === 'function' && !confirm(`Reverse dispatch for transfer #${transferId}? This creates a permanent reversal movement.`)) return false;
      return performAction(transferId, 'corrections/dispatch/reverse', {
        request_token: requestToken(), note: String(note).trim(), confirm_physical_return: true,
      });
    }

    async function setStatus(status) {
      state.status = status || '';
      state.page = 1;
      return loadPage(1);
    }

    async function setPage(page) {
      const next = Math.max(1, Math.min(Number(page) || 1, Math.max(state.totalPages, 1)));
      return loadPage(next);
    }

    return {
      state, open, loadPage, setPage, setStatus, updateForm, refreshAvailability,
      createTransfer, approve, dispatch, openReceipt, closeReceipt, receive, reverseDispatch,
    };
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
  }

  function displayStatus(value) {
    return ({
      draft: 'Draft', approved: 'Approved', in_transit: 'In Transit',
      partially_received: 'Partially Received', received: 'Received', cancelled: 'Cancelled / Reversed',
    })[value] || value || 'Unknown';
  }

  function displayQuantity(value) {
    const number = Number(value || 0);
    return Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: 6 }) : escapeHtml(value);
  }

  function attach({ api, getProgram, isAdmin, confirm }) {
    const root = document.getElementById('siteTransferView');
    if (!root) return null;

    const element = id => document.getElementById(id);
    const setText = (id, value) => { const target = element(id); if (target) target.textContent = value; };
    const optionList = (rows, selected, placeholder, labelFn) =>
      `<option value="">${escapeHtml(placeholder)}</option>${rows.map(row =>
        `<option value="${Number(row.id)}"${String(row.id) === String(selected) ? ' selected' : ''}>${escapeHtml(labelFn(row))}</option>`
      ).join('')}`;

    function renderGate(state) {
      const panel = element('siteTransferGate');
      const workspace = element('siteTransferWorkspace');
      const visible = state.gate === 'enabled';
      if (workspace) workspace.classList.toggle('hidden', !visible);
      if (!panel) return;
      panel.classList.toggle('hidden', visible);
      panel.className = `site-transfer-gate ${state.gate === 'disabled' ? 'is-disabled' : state.gate === 'unavailable' || state.gate === 'denied' ? 'is-error' : ''}${visible ? ' hidden' : ''}`;
      setText('siteTransferGateText', state.gateMessage || 'Checking transfer readiness…');
      const retry = element('siteTransferGateRetry');
      if (retry) retry.classList.toggle('hidden', !['unavailable', 'denied'].includes(state.gate));
    }

    function renderForm(state) {
      const from = element('siteTransferFromSite');
      const to = element('siteTransferToSite');
      const product = element('siteTransferProduct');
      if (!from || !to || !product) return;
      from.innerHTML = optionList(state.sites, state.form.fromSiteId, 'Select source site', row => row.name);
      to.innerHTML = `<option value="">Select destination site</option>${state.sites.map(row =>
        `<option value="${Number(row.id)}"${String(row.id) === String(state.form.toSiteId) ? ' selected' : ''}${String(row.id) === String(state.form.fromSiteId) ? ' disabled' : ''}>${escapeHtml(row.name)}</option>`
      ).join('')}`;
      product.innerHTML = optionList(state.products, state.form.productId, 'Select material', row => `${row.name}${row.sku ? ` · ${row.sku}` : ''}`);
      const selectedProduct = state.products.find(row => String(row.id) === String(state.form.productId));
      const uom = element('siteTransferUom');
      if (uom) uom.value = selectedProduct?.uom || '';
      for (const [name, value] of Object.entries({
        quantity: state.form.quantity,
        reference: state.form.reference,
        notes: state.form.notes,
      })) {
        const input = root.querySelector(`[name="${name}"]`);
        if (input && input.value !== value) input.value = value;
      }
      const available = element('siteTransferAvailable');
      if (available) available.value = state.availabilityLoading ? 'Loading…' : displayQuantity(state.availableQty);
      const availabilityHint = element('siteTransferAvailabilityHint');
      if (availabilityHint) availabilityHint.textContent = state.availabilityError || (state.availabilityLoaded ? 'Current Site Inventory balance' : 'Select a source site and material.');
      const submit = element('siteTransferCreateButton');
      if (submit) submit.disabled = state.busy || state.availabilityLoading || !state.availabilityLoaded || !state.form.fromSiteId || !state.form.toSiteId || !state.form.productId;
    }

    function renderRows(state) {
      const table = element('siteTransferTable');
      if (!table) return;
      if (!state.transfers.length) {
        table.innerHTML = '<div class="empty">No transfers on this page.</div>';
      } else {
        const rows = state.transfers.map(transfer => {
          const items = transfer.items?.length ? transfer.items : [{ product: '—', uom: '', sent_qty: '0', received_qty: '0', in_transit_qty: '0' }];
          return items.map((item, index) => {
            const group = (content) => index === 0 ? `<td rowspan="${items.length}">${content}</td>` : '';
            const actions = index === 0
              ? transfer.status === 'draft'
                ? `<button class="btn soft" type="button" data-site-transfer-action="approve" data-transfer-id="${Number(transfer.id)}">Approve</button>`
                : transfer.status === 'approved'
                  ? `<button class="btn primary" type="button" data-site-transfer-action="dispatch" data-transfer-id="${Number(transfer.id)}">Dispatch</button>`
                  : ''
              : '';
            const receive = ['in_transit', 'partially_received'].includes(transfer.status)
              ? `<button class="btn soft" type="button" data-site-transfer-action="receive" data-transfer-id="${Number(transfer.id)}" data-item-id="${Number(item.id)}">Receive</button>`
              : '';
            const created = transfer.created_at ? new Date(transfer.created_at).toLocaleString() : '—';
            return `<tr>
              ${group(`<strong>#${Number(transfer.id)}</strong>`)}
              ${group(escapeHtml(transfer.from_site))}${group(escapeHtml(transfer.to_site))}
              <td>${escapeHtml(item.product)}${item.sku ? `<small class="site-transfer-cell-sub">${escapeHtml(item.sku)}</small>` : ''}</td>
              <td>${displayQuantity(item.sent_qty)} ${escapeHtml(item.uom)}</td>
              <td>${displayQuantity(item.received_qty)} ${escapeHtml(item.uom)}</td>
              <td>${displayQuantity(item.in_transit_qty)} ${escapeHtml(item.uom)}</td>
              ${group(`<span class="site-transfer-status status-${escapeHtml(transfer.status)}">${escapeHtml(displayStatus(transfer.status))}</span>`)}
              ${group(escapeHtml(transfer.created_by))}${group(escapeHtml(created))}
              ${index === 0 ? `<td rowspan="${items.length}" class="site-transfer-actions">${actions}</td>` : ''}
              <td>${receive}</td>
            </tr>`;
          }).join('');
        }).join('');
        table.innerHTML = `<div class="table-wrap site-transfer-table-wrap"><table class="site-transfer-table"><thead><tr>
          <th>Transfer ID</th><th>From Site</th><th>To Site</th><th>Product</th><th>Sent Qty</th><th>Received Qty</th><th>In Transit Qty</th><th>Status</th><th>Created By</th><th>Created Date</th><th colspan="2">Actions</th>
        </tr></thead><tbody>${rows}</tbody></table></div>`;
      }
      const meta = element('siteTransferPageMeta');
      if (meta) meta.textContent = `${Number(state.total).toLocaleString()} transfers · Page ${state.page} of ${state.totalPages || 1}`;
      const previous = element('siteTransferPrevious');
      const next = element('siteTransferNext');
      if (previous) previous.disabled = state.busy || state.loading || state.page <= 1;
      if (next) next.disabled = state.busy || state.loading || state.page >= state.totalPages;
      const filter = element('siteTransferStatusFilter');
      if (filter && filter.value !== state.status) filter.value = state.status;
    }

    function renderReceipt(state) {
      const dialog = element('siteTransferReceiveDialog');
      const receipt = state.receipt;
      if (!dialog) return;
      if (receipt && !dialog.open) dialog.showModal();
      if (!receipt && dialog.open) dialog.close();
      if (!receipt) return;
      setText('siteTransferReceiveTitle', `Receive transfer #${receipt.transferId}`);
      setText('siteTransferReceiveProduct', `${receipt.product} · ${displayQuantity(receipt.remaining)} ${receipt.uom} remaining`);
      const remaining = element('siteTransferReceiveRemaining');
      if (remaining) remaining.value = `${displayQuantity(receipt.remaining)} ${receipt.uom}`;
      const qty = element('siteTransferReceiveQty');
      if (qty) {
        qty.max = String(receipt.remaining);
        qty.value = receipt.quantity;
      }
      const uom = element('siteTransferReceiveUom');
      if (uom) uom.value = receipt.uom;
      const submit = element('siteTransferReceiveSubmit');
      if (submit) submit.disabled = state.busy;
    }

    function renderCorrections(state) {
      const select = element('siteTransferCorrectionSelect');
      if (!select) return;
      const current = select.value;
      const candidates = state.transfers.filter(row => row.status === 'in_transit');
      select.innerHTML = optionList(candidates, current, 'Select transfer in transit', row => `#${row.id} · ${row.from_site} → ${row.to_site}`);
      const selected = candidates.find(row => String(row.id) === String(select.value));
      const button = element('siteTransferReverseDispatchButton');
      if (button) button.disabled = state.busy || !selected;
    }

    function render(state) {
      renderGate(state);
      renderForm(state);
      renderRows(state);
      renderReceipt(state);
      renderCorrections(state);
      const error = element('siteTransferError');
      if (error) {
        error.textContent = state.error;
        error.classList.toggle('hidden', !state.error);
      }
      const message = element('siteTransferSuccess');
      if (message) {
        message.textContent = state.message;
        message.classList.toggle('hidden', !state.message);
      }
      const refresh = element('siteTransferRefresh');
      if (refresh) refresh.disabled = state.busy || state.loading;
      const submit = element('siteTransferCreateButton');
      if (submit && state.busy) submit.disabled = true;
      root.querySelectorAll('[data-site-transfer-action]').forEach(button => { button.disabled = state.busy || state.loading; });
    }

    const controller = createController({ api, getProgram, isAdmin, onChange: render, confirm });

    root.addEventListener('change', event => {
      const target = event.target;
      if (target.id === 'siteTransferFromSite') controller.updateForm('fromSiteId', target.value);
      else if (target.id === 'siteTransferToSite') controller.updateForm('toSiteId', target.value);
      else if (target.id === 'siteTransferProduct') controller.updateForm('productId', target.value);
      else if (target.id === 'siteTransferStatusFilter') controller.setStatus(target.value);
      else if (target.id === 'siteTransferCorrectionSelect') renderCorrections(controller.state);
    });

    root.addEventListener('input', event => {
      const target = event.target;
      if (target.name === 'receiptQuantity' && target.form?.id === 'siteTransferReceiveForm' && controller.state.receipt) controller.state.receipt.quantity = target.value;
      else if (target.name === 'receiptReference' && controller.state.receipt) controller.state.receipt.reference = target.value;
      else if (target.name === 'receiptNotes' && controller.state.receipt) controller.state.receipt.notes = target.value;
      else if (target.form?.id === 'siteTransferCreateForm' && Object.prototype.hasOwnProperty.call(controller.state.form, target.name)) controller.state.form[target.name] = target.value;
    });

    root.addEventListener('submit', async event => {
      if (event.target.id === 'siteTransferCreateForm') {
        event.preventDefault();
        const form = event.target;
        await controller.createTransfer({
          quantity: form.elements.quantity.value,
          reference: form.elements.reference.value,
          notes: form.elements.notes.value,
        });
      } else if (event.target.id === 'siteTransferReceiveForm') {
        event.preventDefault();
        await controller.receive();
      } else if (event.target.id === 'siteTransferCorrectionForm') {
        event.preventDefault();
        const id = element('siteTransferCorrectionSelect')?.value;
        const note = element('siteTransferCorrectionNote')?.value || '';
        const confirmed = Boolean(element('siteTransferPhysicalReturn')?.checked);
        await controller.reverseDispatch(id, { note, confirmPhysicalReturn: confirmed });
      }
    });

    root.addEventListener('click', async event => {
      const button = event.target.closest('[data-site-transfer-action]');
      if (button) {
        event.preventDefault();
        const id = Number(button.dataset.transferId);
        if (button.dataset.siteTransferAction === 'approve') await controller.approve(id);
        else if (button.dataset.siteTransferAction === 'dispatch') await controller.dispatch(id);
        else if (button.dataset.siteTransferAction === 'receive') controller.openReceipt(id, Number(button.dataset.itemId));
        return;
      }
      const action = event.target.closest('[data-site-transfer-command]')?.dataset.siteTransferCommand;
      if (action === 'refresh') await controller.open();
      else if (action === 'previous') await controller.setPage(controller.state.page - 1);
      else if (action === 'next') await controller.setPage(controller.state.page + 1);
      else if (action === 'close-receipt') controller.closeReceipt();
      else if (action === 'retry-gate') await controller.open();
    });

    element('siteTransferReceiveDialog')?.addEventListener('close', () => {
      if (controller.state.receipt) controller.closeReceipt();
    });

    return controller;
  }

  return { PAGE_SIZE, quantityUnits, validateReceiptQuantity, validateTransferForm, displayStatus, createController, attach };
});
