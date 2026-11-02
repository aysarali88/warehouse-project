import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../static/materials_inventory.html', import.meta.url), 'utf8');
const helper = html.match(/function applyFiberMapReference\(data\)\{[\s\S]*?\n\}/)?.[0];

assert.ok(helper, 'fiber-map dashboard reference merger should be present');

function mergedPlan(dynamic, { city = 'Tripoli', area = 'Mansoura', cableTargetOverride = 39925, fixedCableTarget = false } = {}) {
  const context = vm.createContext({
    warehouseFiberReference: null,
    warehouseFiberReferenceRevision: '',
    ROLLOUT_DASHBOARD_AREAS: { [city]: [area] },
    ROLLOUT_DASHBOARD_PLAN: [{
      city,
      area,
      start: '2026-11-01',
      end: '2026-11-21',
      targetCableMeters: 39925,
      targetSubEndBox: 415,
      targetHubBox: 31,
      targetXbox: 2,
      targetUsers: 2704,
      dashboardCableTargetOverride: cableTargetOverride,
      ...(fixedCableTarget ? { dashboardCableTargetOverrideFixed: true } : {}),
      dashboardSubEndTargetOverride: 415,
      dashboardHubTargetOverride: 31,
      dashboardXboxTargetOverride: 2,
      dashboardSingleCoreTargetOverride: 415,
      dashboardUsersTargetOverride: 2704,
    }],
    HIDDEN_DYNAMIC_MAP_AREAS: new Set(),
    WFM_MAP_SCOPES: [],
    wfmMapAreaKey: value => String(value || '').toLowerCase().replace(/[^a-z0-9]/g, ''),
    wfmAreaMatches: () => false,
    wfmXboxKey: value => String(value || '').toUpperCase(),
    wfmValue: () => '',
  });

  vm.runInContext(helper, context);
  vm.runInContext(`applyFiberMapReference({
    revision: 'r2',
    boxes: [],
    routes: [],
    area_plans: [{
      city: '${city}', area: '${area}', dynamic: ${dynamic},
      start: '2026-11-01', end: '2026-11-21', targetCableMeters: 40125,
      targetSubEndBox: 417, targetHubBox: 32, targetXbox: 2, targetUsers: 2704
    }]
  })`, context);
  return vm.runInContext('ROLLOUT_DASHBOARD_PLAN[0]', context);
}

test('Area Builder map targets take precedence over stale static Mansoura targets', () => {
  const plan = mergedPlan(true);

  assert.equal(plan.targetCableMeters, 40125);
  assert.equal(plan.targetSubEndBox, 417);
  assert.equal(plan.targetHubBox, 32);
  assert.equal(plan.targetXbox, 2);
  assert.equal(Object.hasOwn(plan, 'dashboardCableTargetOverride'), false);
  assert.equal(Object.hasOwn(plan, 'dashboardSubEndTargetOverride'), false);
  assert.equal(Object.hasOwn(plan, 'dashboardHubTargetOverride'), false);
  assert.equal(Object.hasOwn(plan, 'dashboardXboxTargetOverride'), false);
  assert.equal(Object.hasOwn(plan, 'dashboardSingleCoreTargetOverride'), false);
  assert.equal(Object.hasOwn(plan, 'dashboardUsersTargetOverride'), false);
});

test('non-dynamic map references keep intentionally configured dashboard targets', () => {
  const plan = mergedPlan(false);

  assert.equal(plan.targetCableMeters, 40125);
  assert.equal(plan.dashboardCableTargetOverride, 39925);
  assert.equal(plan.dashboardSubEndTargetOverride, 415);
});

test('a fixed completed-area cable target survives dynamic map refresh', () => {
  const plan = mergedPlan(true, { city: 'Misurata', area: 'Ras Ali', cableTargetOverride: 22520, fixedCableTarget: true });

  assert.equal(plan.dashboardCableTargetOverride, 22520);
  assert.equal(plan.dashboardCableTargetOverrideFixed, true);
  assert.equal(plan.targetSubEndBox, 417);
  assert.equal(plan.targetHubBox, 32);
  assert.equal(plan.targetXbox, 2);
  assert.equal(plan.targetUsers, 2704);
});
