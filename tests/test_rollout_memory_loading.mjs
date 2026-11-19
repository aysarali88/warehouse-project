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

test('City Overview keeps the compact ranking endpoint to avoid loading the larger Rollout summary', () => {
  const tabSwitch = html.match(/function switchRolloutDailyTab\([^\n]+/)?.[0];
  const overviewBranch = tabSwitch?.match(/if\(rolloutOpen&&tab==='overview'\)\{([^}]*)\}/)?.[1] || '';
  assert.match(overviewBranch, /refreshRolloutCityRanking\(false\)/);
  assert.doesNotMatch(overviewBranch, /refreshRolloutDailyProgress|ensureRolloutDashboardDesignReference/);
  const refresh = html.match(/async function refreshRolloutCityRanking\([\s\S]*?(?=\nfunction rolloutCityOverviewMetricFromSummary\()/)?.[0] || '';
  assert.match(refresh, /rollout-city-ranking-summary/);
  assert.doesNotMatch(refresh, /rollout-dashboard-summary|rollout-daily-progress|fiber-map-reference/);
});

test('City Overview contains only the Productivity Ranking panel', () => {
  const renderer = html.match(/function renderRolloutCityOverview\(\)\{[\s\S]*?(?=\nfunction renderRolloutDailyProgress\()/)?.[0] || '';
  assert.match(renderer, /Productivity Ranking/);
  assert.match(renderer, /rollout-city-ranking-group/);
  assert.doesNotMatch(renderer, /rollout-city-total-grid|rollout-city-hero|rollout-city-area-grid|Timeline vs Plan|Actual users/);
});

test('ranking score keeps the existing 80/20 cable and box weighting', () => {
  const metricSource = html.match(/function rolloutCityOverviewMetricFromSummary\([\s\S]*?\n\}/)?.[0] || '';
  const scoreSource = html.match(/function rolloutDashboardProductivityScore\([\s\S]*?\n\}/)?.[0] || '';
  assert.ok(metricSource);
  const context = vm.createContext({
    rolloutPlanFor: () => ({}),
    isMaqawbaRolloutArea: () => false,
    applyRolloutDashboardMetricOverrides: metric => metric,
    rolloutTargetFor: () => ({ cableMeters: 100, totalBoxes: 10 }),
    canonicalRolloutArea: area => area,
    Math,
  });
  vm.runInContext(`${scoreSource}\n${metricSource}`, context);
  const score = vm.runInContext(`rolloutCityOverviewMetricFromSummary('Tripoli', 'Example', {
    cable_meters: 100, sub_end_box: 5, hub_box: 2, xbox: 1
  }).productivity`, context);
  assert.equal(score, 96);
});

test('Maqawba ranking uses map-confirmed cable progress instead of raw cable totals', () => {
  const metricSource = html.match(/function rolloutCityOverviewMetricFromSummary\([\s\S]*?\n\}/)?.[0] || '';
  const scoreSource = html.match(/function rolloutDashboardProductivityScore\([\s\S]*?\n\}/)?.[0] || '';
  const context = vm.createContext({
    rolloutPlanFor: () => ({}),
    isMaqawbaRolloutArea: area => area === 'Maqawba',
    applyRolloutDashboardMetricOverrides: metric => metric,
    rolloutTargetFor: () => ({ cableMeters: 100, totalBoxes: 10 }),
    canonicalRolloutArea: area => area,
    Math,
  });
  vm.runInContext(`${scoreSource}\n${metricSource}`, context);
  const score = vm.runInContext(`rolloutCityOverviewMetricFromSummary('Misurata', 'Maqawba', {
    cable_meters: 300, map_cable_meters: 50, map_cable_target_meters: 100,
    map_cable_progress_available: true, sub_end_box: 5, hub_box: 2, xbox: 1
  }).productivity`, context);
  assert.equal(score, 56);
});

test('Dashboard and Productivity Ranking share the same 80/20 scoring helper and target source', () => {
  const dashboardRenderer = html.match(/function renderRolloutDailyProgress\(\)\{[\s\S]*?(?=\nfunction )/)?.[0] || '';
  const overviewMetric = html.match(/function rolloutCityOverviewMetricFromSummary\([\s\S]*?\n\}/)?.[0] || '';
  const scoreHelper = html.match(/function rolloutDashboardProductivityScore\([\s\S]*?\n\}/)?.[0] || '';
  assert.match(dashboardRenderer, /rolloutDashboardAreaMetric\(rolloutDashboardFilters\.city,area,rows\)/);
  assert.match(overviewMetric, /rolloutDashboardProductivityScore\(metric,finalTarget\)/);
  assert.doesNotMatch(overviewMetric, /row\.design_target/);
  assert.ok(scoreHelper);

  const context = vm.createContext({
    Math,
    rolloutPlanFor: () => ({}),
    isMaqawbaRolloutArea: () => false,
    applyRolloutDashboardMetricOverrides: metric => metric,
    rolloutTargetFor: () => ({ cableMeters: 39925, totalBoxes: 448 }),
    canonicalRolloutArea: area => area,
  });
  vm.runInContext(`${scoreHelper}\n${overviewMetric}`, context);
  const scores = vm.runInContext(`(() => {
    const summary = rolloutCityOverviewMetricFromSummary('Tripoli', 'Al-Mansoura', {
      cable_meters: 39925, sub_end_box: 415, hub_box: 31, xbox: 2,
      design_target: { cableMeters: 50000, totalBoxes: 500 }
    });
    const dashboard = rolloutDashboardProductivityScore(
      { cableMeters: 39925, totalBoxes: 448 },
      { cableMeters: 39925, totalBoxes: 448 }
    );
    return [summary.productivity, dashboard.productivity];
  })()`, context);
  assert.deepEqual(Array.from(scores), [100, 100]);
});

test('Manqosh project timeline uses the existing start date and updated November 23 end date', () => {
  assert.match(html, /\{city:'Misurata',area:'Manqosh',start:'2026-11-04',end:'2026-11-23'\}/);
});

test('area report XBOX pivot is calculated from its compact report rows', () => {
  const start = html.indexOf('function rolloutPivotData(');
  const end = html.indexOf('\n}', start);
  assert.notEqual(start, -1);
  const pivot = html.slice(start, end);
  assert.match(pivot, /rolloutAreaInstalledMetrics\(area,source\)\.xbox/);
});
