// Isolated UI checks: every API request is intercepted; no market or notification
// service is contacted. Set ALPHA_UI_PLAYWRIGHT_MODULE to an installed
// playwright-core/index.mjs when Playwright is not a local dependency.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createServer } from 'vite';

const { chromium } = process.env.ALPHA_UI_PLAYWRIGHT_MODULE
  ? await import(pathToFileURL(process.env.ALPHA_UI_PLAYWRIGHT_MODULE).href)
  : await import('playwright-core');
const root = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(new URL('../../.runtime/live-market-ui/', import.meta.url));
await mkdir(output, { recursive: true });
const stamp = '2026-10-22T02:30:30+00:00';
const quoteData = {
  market: 'sh', symbol: '600519', name: '行情界面样例', price: '103', previous_close: '100',
  change: '3', change_pct: '3', source_reported_at: stamp, received_at: stamp,
  source: '界面测试夹具', source_url: '', price_unit: '元', change_pct_unit: '%',
};
const quote = {
  data: quoteData, data_state: 'fresh', data_label: '近期报价', source_age_seconds: 20,
  refresh_state: 'succeeded', last_attempt_at: stamp, error_message: null,
  retry_after_seconds: 0, next_refresh_at: null, market_state: 'trading', realtime_verified: true,
  eligibility: { eligible: true, code: 'ready', reason: '来源时间与成交量均有增加，可用于判断。', checked_at: stamp, session_id: '2026-10-22-am', proof: null },
};
const initialRule = {
  market: 'sh', symbol: '600519', watchlist_created_at: stamp,
  up_threshold: '3', down_threshold: '3', enabled: 0, version: 1,
  updated_at: stamp, last_checked_at: stamp, last_result: '规则未开启。',
};
const item = { market: 'sh', symbol: '600519', name: '行情界面样例', source: '界面测试夹具', verified_at: stamp, created_at: stamp, quote, rule: initialRule };
const waitingItem = {
  ...item, market: 'sz', symbol: '000001', name: '成交待确认样例',
  rule: { ...initialRule, market: 'sz', symbol: '000001', enabled: 1, last_result: '待确认成交活动。' },
  quote: { ...quote, realtime_verified: false, eligibility: { ...quote.eligibility, eligible: false, code: 'activity_wait', reason: '待确认成交活动：还没有观察到成交量增加，暂不判断。' } },
};
function event(mode) {
  return {
    id: `${mode}-ui-example`, mode, market: 'sh', symbol: '600519', direction: 'up', trade_date: '2026-10-22',
    created_at: stamp, notification: null,
    snapshot: { quote: quoteData, change_pct: '3', direction: 'up', threshold: '3', rule_version: 1,
      first_observation: false, detection_label: '界面样例', formula: '', repeat_policy: '同方向当日不重复' },
  };
}
const view = {
  items: [item, waitingItem], events: [event('live'), event('demo')], runs: [],
  runtime: { heartbeat_at: stamp, checks: 1, last_error: null, running: true },
  collector: { running: true, heartbeat_at: stamp, last_cycle_at: stamp, next_cycle_at: stamp,
    state: 'waiting', label: '采集运行中', interval_seconds: 60, freshness_seconds: 120,
    total: 2, attempted: 2, succeeded: 2, failed: 0, last_error: null },
  live_ready: true, live_pause_reason: '', check_interval_seconds: 5, demo_freshness_seconds: 120,
};
let storedRule = structuredClone(initialRule);
let failedRead = false;
let readCount = 0;
const saves = [], apiCalls = [], checks = [], pageErrors = [];
const check = (name, value) => { assert.ok(value, name); checks.push(name); };
const server = await createServer({ root, server: { host: '127.0.0.1', port: 5188, strictPort: true }, logLevel: 'error' });
let browser;
try {
  await server.listen();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:5188') return route.abort();
    if (!url.pathname.startsWith('/api/')) return route.continue();
    apiCalls.push({ method: route.request().method(), path: url.pathname });
    const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (url.pathname === '/api/dashboard') return send({ items: view.items.map(({ rule, ...stock }) => stock), total: view.items.length, collector: view.collector });
    if (url.pathname === '/api/access') return send({ mode: 'local', lan_urls: [], release: { app_version: 'UI fixture' } });
    if (url.pathname === '/api/watchlist/groups') return send({ items: [] });
    if (url.pathname === '/api/anomalies') {
      readCount++;
      return failedRead ? send({ error: { message: '界面测试：本次读取中断，请重试。' } }, 503) : send(view);
    }
    if (url.pathname === '/api/anomalies/rules/sh/600519' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      saves.push(body);
      if (body.expected_version !== storedRule.version) return send({ error: { message: '规则版本已更新，请重新读取。' } }, 409);
      storedRule = { ...storedRule, ...body, enabled: Number(body.enabled), version: storedRule.version + 1 };
      // Deliberately keep GET responses behind a completed save to exercise CAS.
      return send({ rule: storedRule });
    }
    return send({ error: { message: `Unexpected fixture request: ${url.pathname}` } }, 500);
  });
  await context.addInitScript(() => {
    window.uiFocusCalls = [];
    window.uiScrollCalls = [];
    const focus = HTMLElement.prototype.focus;
    HTMLElement.prototype.focus = function (...args) { window.uiFocusCalls.push(this.id || this.tagName); return focus.apply(this, args); };
    const scroll = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (...args) { window.uiScrollCalls.push(this.id || this.tagName); return scroll.apply(this, args); };
  });
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(String(error)));
  await page.goto('http://127.0.0.1:5188/', { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '异动检测', exact: false }).first().click();
  const editor = page.getByRole('article', { name: '行情界面样例异动规则' });
  await editor.waitFor();
  const enabled = editor.getByRole('checkbox', { name: /启用真实行情规则/ });
  check('default setting stays off and can be changed', !await enabled.isChecked() && await enabled.isEnabled());
  check('mount and polling do not save or enable rules', saves.length === 0);
  check('per-stock activity reason is visible', (await page.getByRole('article', { name: '成交待确认样例异动规则' }).innerText()).includes('还没有观察到成交量增加'));
  const liveSection = page.locator('section[aria-labelledby="live-events-title"]');
  const demoSection = page.locator('section[aria-labelledby="demo-history-title"]');
  check('real and synthetic event records are separated', await liveSection.locator('.anomaly-mode.live').count() === 1 && await liveSection.locator('.anomaly-mode.demo').count() === 0 && await demoSection.locator('.anomaly-mode.demo').count() === 1);

  await enabled.check();
  check('checking the option alone does not save', saves.length === 0);
  await editor.getByRole('button', { name: '保存规则', exact: true }).click();
  await editor.getByText('规则已保存并开启。', { exact: false }).waitFor();
  check('explicit save sends the current CAS version', saves[0].expected_version === 1 && saves[0].enabled === true && saves[0].watchlist_created_at === stamp);
  const up = editor.getByLabel('上涨达到（%）');
  await up.fill('4');
  check('an older cached response does not create a false conflict', await editor.getByRole('button', { name: '保存规则', exact: true }).isEnabled());
  await editor.getByRole('button', { name: '保存规则', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.anomaly-rule .notification-badge')?.textContent === '规则 v3');
  check('the next save uses the returned version, not stale GET data', saves[1].expected_version === 2 && saves[1].up_threshold === '4');

  await up.fill('5');
  storedRule = { ...storedRule, up_threshold: '6', version: 4 };
  view.items[0].rule = structuredClone(storedRule);
  await editor.getByText('规则已从其他操作更新。', { exact: false }).waitFor();
  check('newer external versions preserve the draft and block overwrite', await up.inputValue() === '5' && await editor.getByRole('button', { name: '保存规则', exact: true }).isDisabled());
  await editor.getByRole('button', { name: '重新读取规则' }).click();
  check('explicit reload accepts the newer saved version', await up.inputValue() === '6' && await enabled.isChecked());

  await up.fill('7');
  await page.evaluate(() => { window.uiFocusCalls = []; window.uiScrollCalls = []; });
  const beforeFailure = readCount;
  failedRead = true;
  await page.getByText('读取失败，下面保留上次结果。', { exact: false }).waitFor();
  const focusState = await page.evaluate(() => ({ active: document.activeElement?.id, focus: window.uiFocusCalls, scroll: window.uiScrollCalls }));
  check('poll errors preserve the draft, focus and old results without scrolling', readCount > beforeFailure && await up.inputValue() === '7' && focusState.active === 'up-sh600519' && focusState.focus.length === 0 && focusState.scroll.length === 0 && await liveSection.locator('.anomaly-event').count() === 1);
  failedRead = false;
  await page.getByRole('button', { name: '重新读取', exact: true }).click();

  view.live_ready = false;
  view.live_pause_reason = '午间休市，暂停真实判断。';
  view.collector.label = '午间休市，采集暂停';
  view.collector.next_cycle_at = null;
  view.items[0].quote = { ...quote, market_state: 'break', eligibility: { ...quote.eligibility, eligible: false, reason: '午间休市，暂停真实判断。' } };
  await page.getByText('午间休市，采集暂停', { exact: true }).waitFor();
  check('non-trading pause replaces the active state', (await editor.locator('.anomaly-rule-status').innerText()).includes('午间休市') && !(await editor.locator('.anomaly-rule-status').innerText()).includes('后台自动检查'));

  view.collector.label = '采集有失败，稍后重试';
  view.collector.failed = 1;
  view.collector.succeeded = 1;
  view.collector.last_error = '部分股票本轮未取得新报价。';
  view.items[0].quote = { ...view.items[0].quote, data_state: 'stale', data_label: '报价已过旧', refresh_state: 'failed', error_message: '本次来源请求未完成。' };
  await page.getByText('采集有失败，稍后重试', { exact: true }).waitFor();
  await page.getByText('本次更新未完成，保留上次报价。', { exact: false }).waitFor();
  check('failed acquisition keeps the previous price and explains the old value', (await page.getByRole('region', { name: '行情界面样例报价' }).innerText()).includes('103.00') && (await page.getByRole('region', { name: '采集与检测状态' }).innerText()).includes('保留上次报价'));

  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await page.locator('.anomaly-runtime-help').evaluate(element => { element.open = true; });
    await editor.locator('.anomaly-demo-controls').evaluate(element => { element.open = true; });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    check(`no horizontal overflow at ${width}px with details open`, !overflow);
    if (width !== 320) await page.locator('#anomaly-details').screenshot({ path: `${output}/anomalies-${width}.png` });
  }
  check('no React or browser runtime errors', pageErrors.length === 0);
  check('no demo, market-refresh or notification POST was sent', apiCalls.filter(call => call.method !== 'GET').every(call => call.path === '/api/anomalies/rules/sh/600519'));
  await writeFile(`${output}/result.json`, JSON.stringify({ ok: true, scope: 'UI fixtures only; not real-market acceptance', checks, pageErrors, saves, apiCalls }, null, 2));
  console.log(JSON.stringify({ ok: true, checks: checks.length, output, scope: 'UI fixtures only; not real-market acceptance' }));
} catch (error) {
  await writeFile(`${output}/result.json`, JSON.stringify({ ok: false, checks, error: String(error), pageErrors, apiCalls }, null, 2));
  throw error;
} finally {
  if (browser) await browser.close();
  await server.close();
}
