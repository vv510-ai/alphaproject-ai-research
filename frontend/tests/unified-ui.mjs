// Interface contract regression only: all API responses are isolated substitutes.
// Run persistent-flow.mjs and the integrated unification flow for real SQLite proof.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createServer } from 'vite';

const { chromium } = process.env.ALPHA_UI_PLAYWRIGHT_MODULE
  ? await import(pathToFileURL(process.env.ALPHA_UI_PLAYWRIGHT_MODULE).href) : await import('playwright-core');
const root = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(new URL('../../evidence/unification-implementation-20261023/', import.meta.url));
await mkdir(output, { recursive: true });
const stamp = '2026-10-23T02:35:00+00:00';
const quote = { data: null, data_state: 'unavailable', data_label: '尚无可用报价', refresh_state: 'idle', retry_after_seconds: 0, market_state: 'closed', eligibility: { eligible: false, reason: '隔离检查，未连接行情。' } };
let items = [
  { market: 'sh', symbol: '600519', name: '验收股票甲', source: '隔离界面样例', verified_at: stamp, created_at: stamp, group_id: null, is_holding: false, display_order: 0, metadata_version: 1, quote },
  { market: 'sz', symbol: '000001', name: '验收股票乙', source: '隔离界面样例', verified_at: stamp, created_at: stamp, group_id: null, is_holding: false, display_order: 1, metadata_version: 1, quote },
];
let groups = [], removed, reorderConflict = false, researchFailure = false, researchReads = 0;
let extendedRule = { market: 'sh', symbol: '600519', watchlist_created_at: stamp, version: 0, enabled: false, config: { limit_enabled: false, breakout_enabled: false, volume_enabled: false, amplitude_enabled: false, event_enabled: false, volume_ratio: '3', amplitude_pct: '8', lookback_days: 60 }, last_checked_at: null, last_results: [], updated_at: stamp };
const calls = [], errors = [], checks = [], blockedExternal = [];
const check = (name, value) => { assert.ok(value, name); checks.push(name); };
const research = {
  scope: 'market', market: null, symbol: null, snapshot_id: 'fixture-snapshot', created_at: stamp, refresh_state: 'idle', retry_after_seconds: 0,
  sections: [
    { key: 'indices', title: '主要指数', status: 'partial', source: '界面样例来源', source_url: 'https://example.com/indices', as_of: stamp, received_at: stamp, error_message: '一项数值缺失。', items: [{ id: 'index-1', name: '测试指数', code: '000001', price: null, change_pct: '-1.25', as_of: stamp, received_at: stamp, source: '界面样例来源', source_url: 'https://example.com/index-1' }] },
    { key: 'global', title: '全球市场', status: 'stale', source: '界面样例来源', source_url: null, as_of: '2026-10-20T02:35:00+00:00', received_at: stamp, error_message: '资料已过期。', items: [] },
    { key: 'sectors', title: '板块对照', status: 'unavailable', source: null, source_url: null, as_of: null, received_at: null, error_message: '尚未取得。', items: [] },
    { key: 'news', title: '财经资讯', status: 'available', source: '界面样例来源', source_url: null, as_of: stamp, received_at: stamp, error_message: null, items: [{ id: 'news-1', title: '公司调整营收预期 10% → 8%', summary: '<script>window.injected=true</script> 作为纯文本展示。', url: 'javascript:window.injected=true', source: '界面样例来源', published_at: stamp, received_at: '2026-10-23T02:40:00+00:00', change_kind: 'updated' }] },
    { key: 'announcements', title: '公司公告', status: 'unavailable', source: null, source_url: null, as_of: null, received_at: null, error_message: '无符合条件的公告。', items: [] },
    { key: 'calendar', title: '财经日历', status: 'partial', source: '界面日历来源', source_url: null, as_of: stamp, received_at: stamp, error_message: null, items: [{ id: 'calendar-1', title: '未来日程示例', url: 'https://example.com/calendar', source: '界面日历来源', published_at: null, scheduled_at: '2026-10-25T08:00:00+00:00', received_at: stamp, eligibility_note: '发布时间未提供，仅作日程展示' }] },
  ],
};
const archive = [
  { id: 'old-1', source_id: 'original-1', kind: 'notification', occurred_at: stamp, symbol: '600519', status: 'success', content: { title: '旧通知内容', message: '原始内容保留' } },
  { id: 'old-2', source_id: 'original-2', kind: 'undelivered', occurred_at: stamp, symbol: null, status: 'pending', content: { title: '未发送旧队列' } },
];
const server = await createServer({ root, server: { host: '127.0.0.1', port: 5189, strictPort: true }, logLevel: 'error' });
let browser, page;
try {
  await server.listen();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url()), method = route.request().method();
    if (url.origin !== 'http://127.0.0.1:5189') { blockedExternal.push(url.href); return route.abort(); }
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const body = route.request().postDataJSON();
    calls.push({ path: url.pathname, query: url.search, method, body });
    const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (url.pathname === '/api/dashboard') return send({ items, total: items.length });
    if (url.pathname === '/api/access') return send({ mode: 'public', public_url: 'https://example.com/', lan_urls: [], release: { app_version: 'isolated-ui' } });
    if (url.pathname === '/api/watchlist/groups') {
      if (method === 'POST') groups.push({ id: 'group-1', name: body.name, created_at: stamp });
      return send({ items: groups });
    }
    if (url.pathname.startsWith('/api/watchlist/groups/')) {
      if (method === 'PATCH') groups[0].name = body.name;
      if (method === 'DELETE') { groups = []; items = items.map(item => ({ ...item, group_id: null, metadata_version: item.metadata_version + 1 })); }
      return send({ removed: true });
    }
    if (url.pathname === '/api/watchlist/reorder') {
      if (reorderConflict) { reorderConflict = false; return send({ error: { message: '列表已由其他操作更新，请重新读取。' } }, 409); }
      assert.equal(body.items.length, items.length);
      for (const value of body.items) { const item = items.find(item => item.symbol === value.symbol); assert.equal(value.metadata_version, item.metadata_version); assert.equal(value.watchlist_created_at, item.created_at); }
      items = body.items.map((value, index) => ({ ...items.find(item => item.symbol === value.symbol), display_order: index, metadata_version: value.metadata_version + 1 }));
      return send({ items, total: items.length });
    }
    if (url.pathname === '/api/watchlist/undo/test-token') {
      items.push({ ...removed, created_at: '2026-10-23T02:45:00+00:00', metadata_version: 1 });
      extendedRule.watchlist_created_at = items.at(-1).created_at;
      return send({ item: items.at(-1), restored: true, rule_restored_disabled: true });
    }
    if (/^\/api\/watchlist\/(sh|sz)\/\d{6}$/.test(url.pathname)) {
      const symbol = url.pathname.split('/').at(-1), item = items.find(item => item.symbol === symbol);
      if (method === 'PATCH') {
        assert.equal(body.expected_version, item.metadata_version); assert.equal(body.watchlist_created_at, item.created_at);
        items = items.map(row => row.symbol === symbol ? { ...row, group_id: body.group_id, is_holding: body.is_holding, metadata_version: row.metadata_version + 1 } : row);
        return send({ item: items.find(row => row.symbol === symbol) });
      }
      assert.equal(url.searchParams.get('watchlist_created_at'), item.created_at);
      removed = structuredClone(item); items = items.filter(row => row.symbol !== symbol);
      return send({ removed: true, undo_token: 'test-token', undo_expires_at: new Date(Date.now() + 60_000).toISOString() });
    }
    if (url.pathname.startsWith('/api/research')) {
      if (method === 'POST') {
        if (researchFailure) return send({ error: { message: '隔离故障：来源请求失败。' } }, 503);
        research.refresh_state = 'processing'; researchReads = 0; return send(research, 202);
      }
      if (research.refresh_state === 'processing' && ++researchReads >= 1) research.refresh_state = 'succeeded';
      return send(research);
    }
    if (url.pathname === '/api/legacy/migration') return send({ status: 'completed', imported_at: stamp, kinds: { notification: 1, undelivered: 1 }, watchlist: { imported: 1, existing: 1 }, warnings: ['旧分组名称待确认。'], source_fingerprint: 'fixture-source' });
    if (url.pathname === '/api/legacy/history') {
      const list = archive.filter(item => (!url.searchParams.get('kind') || item.kind === url.searchParams.get('kind')) && (!url.searchParams.get('symbol') || item.symbol === url.searchParams.get('symbol')));
      return send({ items: list, total: list.length, next_cursor: null });
    }
    if (url.pathname === '/api/anomalies') return send({ items: items.map(item => ({ ...item, rule: null })), events: [], runs: [], collector: null, runtime: { running: false, checks: 0 }, live_ready: false, live_pause_reason: '隔离UI数据', check_interval_seconds: 5 });
    if (url.pathname === '/api/anomalies/extended/sh/600519') {
      if (method === 'PUT') { assert.equal(body.expected_version, extendedRule.version); assert.equal(body.watchlist_created_at, extendedRule.watchlist_created_at); extendedRule = { ...extendedRule, ...body, version: extendedRule.version + 1 }; }
      return send(extendedRule);
    }
    return send({ error: { message: `Unexpected fixture API: ${url.pathname}` } }, 500);
  });
  page = await context.newPage(); page.setDefaultTimeout(12000);
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto('http://127.0.0.1:5189/', { waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: '关注的股票', exact: false }).waitFor();
  check('watchlist remains the home and every stock has four connected entries', await page.getByRole('link', { name: '自选股票', exact: false }).getAttribute('aria-current') === 'page' && await page.getByRole('button', { name: '设置验收股票甲提醒' }).count() === 1 && await page.getByRole('button', { name: '查看验收股票甲资讯与公告' }).count() === 1);
  await page.locator('.watch-groups > summary').click();
  await page.getByRole('button', { name: '创建分组', exact: true }).click();
  await page.getByText('请填写分组名称。', { exact: true }).waitFor();
  check('empty group name is rejected without a write', !calls.some(call => call.path === '/api/watchlist/groups' && call.method === 'POST'));
  await page.getByLabel('新分组名称', { exact: true }).fill('长期关注');
  await page.getByRole('button', { name: '创建分组', exact: true }).click();
  await page.getByLabel('验收股票甲分组', { exact: true }).selectOption('group-1');
  await page.getByText('验收股票甲的分组已保存。', { exact: true }).waitFor();
  await page.getByRole('checkbox', { name: '标记验收股票甲为持仓', exact: true }).check();
  await page.getByText('验收股票甲的持仓标记已保存。', { exact: true }).waitFor();
  check('group and holding changes submit current versions and retain both fields', items[0].group_id === 'group-1' && items[0].is_holding && items[0].metadata_version === 3);
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByLabel('验收股票甲分组', { exact: true }).waitFor();
  check('reload reads the saved group and holding state', await page.getByLabel('验收股票甲分组', { exact: true }).inputValue() === 'group-1' && await page.getByRole('checkbox', { name: '标记验收股票甲为持仓', exact: true }).isChecked());
  await page.getByRole('checkbox', { name: '只看持仓标记', exact: true }).check();
  check('holding filter hides other stocks and prevents ambiguous filtered sorting', await page.locator('.stock-row').count() === 1 && await page.getByRole('button', { name: '下移验收股票甲', exact: true }).isDisabled());
  await page.getByRole('button', { name: '显示全部', exact: true }).click();
  await page.getByRole('button', { name: '下移验收股票甲', exact: true }).click();
  await page.getByText('已下移验收股票甲，顺序已保存。', { exact: true }).waitFor();
  check('ordering uses the full list and returns saved order', items[0].symbol === '000001' && (await page.locator('.stock-row').first().innerText()).includes('验收股票乙'));
  reorderConflict = true;
  await page.getByRole('button', { name: '上移验收股票甲', exact: true }).click();
  await page.getByText('顺序未保存：', { exact: false }).waitFor();
  check('a version conflict keeps the server order and gives a recovery path', items[0].symbol === '000001' && (await page.locator('.stock-row').first().innerText()).includes('验收股票乙'));
  await page.getByRole('button', { name: '移除验收股票甲', exact: true }).click();
  await page.getByRole('button', { name: '确认移除验收股票甲', exact: true }).click();
  await page.getByRole('button', { name: '撤销移除', exact: true }).waitFor();
  check('delete carries the exact watchlist generation and exposes undo', items.length === 1 && calls.find(call => call.method === 'DELETE' && call.path.endsWith('/600519')).query.includes('watchlist_created_at='));
  await page.getByRole('button', { name: '撤销移除', exact: true }).click();
  await page.getByText('原有规则设置已恢复为关闭', { exact: false }).waitFor();
  check('undo restores metadata as a new generation and explicitly keeps rules off', items.length === 2 && items[1].created_at !== stamp && items[1].is_holding && items[1].group_id === 'group-1');
  await page.getByRole('button', { name: '设置验收股票甲提醒', exact: true }).click();
  await page.getByText('当前查看：验收股票甲', { exact: false }).waitFor();
  check('single-stock alert entry only exposes the selected rule', await page.locator('.anomaly-rule').count() === 1 && (await page.locator('.anomaly-rule').innerText()).includes('验收股票甲'));
  await page.locator('.extended-rule > summary').click();
  const extended = page.locator('.extended-rule');
  const toggle = extended.getByRole('checkbox', { name: '启用扩展提醒', exact: false });
  await toggle.waitFor();
  await page.waitForFunction(() => !document.querySelector('.extended-rule input[type="checkbox"]').disabled);
  check('extended rules load with every trigger off and do not write on open', await extended.getByRole('checkbox', { checked: true }).count() === 0 && !calls.some(call => call.path.startsWith('/api/anomalies/extended') && call.method === 'PUT'));
  await toggle.check();
  await extended.getByRole('checkbox', { name: '振幅异常', exact: false }).check();
  await extended.getByLabel('振幅阈值（%）', { exact: true }).fill('9');
  check('editing extended choices alone makes no saved change', !extendedRule.enabled && !extendedRule.config.amplitude_enabled);
  await extended.getByRole('button', { name: '保存扩展提醒', exact: true }).click();
  await extended.getByText('扩展提醒设置已保存。', { exact: false }).waitFor();
  check('explicit extended save binds the current generation and version', extendedRule.enabled && extendedRule.config.amplitude_enabled && extendedRule.config.amplitude_pct === '9' && extendedRule.version === 1);
  await extended.getByLabel('历史对照天数', { exact: true }).fill('4');
  await extended.getByRole('button', { name: '保存扩展提醒', exact: true }).click();
  await extended.getByText('历史对照天数请填写 5 至 120 之间的整数。', { exact: true }).waitFor();
  check('invalid extended lookback is rejected before a write', extendedRule.version === 1);
  for (const width of [390, 320]) { await page.setViewportSize({ width, height: 900 }); check(`open extended settings do not overflow at ${width}px`, await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)); }
  await page.locator('#anomaly-details').screenshot({ path: `${output}ui-extended-320.png` });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('button', { name: '收起异动检测', exact: true }).click();
  await page.getByRole('button', { name: '查看验收股票甲资讯与公告', exact: true }).click();
  await page.locator('.stock-research').getByRole('heading', { name: '财经资讯', exact: true }).waitFor();
  check('single-stock research reads its scoped endpoint without a refresh', calls.some(call => call.path === '/api/research/sh/600519' && call.method === 'GET') && !calls.some(call => call.path.startsWith('/api/research') && call.method === 'POST'));
  await page.locator('.stock-research').getByRole('button', { name: '收起资料', exact: true }).click();
  await page.getByRole('link', { name: '综合行情', exact: true }).focus(); await page.keyboard.press('Enter');
  await page.getByRole('heading', { name: '主要指数', exact: true }).waitFor();
  check('market navigation is keyboard-operable and null values stay unknown', await page.getByRole('link', { name: '综合行情', exact: true }).getAttribute('aria-current') === 'page' && (await page.locator('.research-quote-values').innerText()).includes('待确认') && (await page.locator('.research-quote-values').innerText()).includes('-1.25%'));
  await page.getByRole('button', { name: '更新资料', exact: true }).click();
  await page.getByRole('button', { name: '更新资料', exact: true }).waitFor();
  check('background refresh polls saved state until completion', calls.some(call => call.path === '/api/research/refresh' && call.method === 'POST') && research.refresh_state === 'succeeded');
  researchFailure = true;
  await page.getByRole('button', { name: '更新资料', exact: true }).click();
  await page.getByText('隔离故障：来源请求失败。', { exact: false }).waitFor();
  check('failed refresh keeps the previous market data visible', (await page.locator('.research-quote-values').innerText()).includes('-1.25%'));
  await page.getByRole('link', { name: '资讯与日历', exact: true }).click();
  await page.getByRole('heading', { name: '财经资讯', exact: true }).waitFor();
  check('news source text is inert and unsafe URLs are not clickable', await page.getByRole('link', { name: '公司调整营收预期 10% → 8%', exact: true }).count() === 0 && await page.evaluate(() => !window.injected) && (await page.locator('.research-summary').innerText()).includes('<script>'));
  check('publication, receipt, scheduled time and changed content stay distinct', (await page.locator('.research-panel').innerText()).includes('日程时间：2026/10/25') && (await page.locator('.research-panel').innerText()).includes('发布时间未提供，仅作日程展示') && await page.getByText('有更新', { exact: true }).count() === 1);
  await page.getByRole('link', { name: '旧版历史档案', exact: true }).click();
  await page.getByText('旧版记录成功（未确认本人实收）', { exact: true }).waitFor();
  check('legacy success is not promoted to human receipt and unsent queue is read-only', (await page.locator('.legacy-panel').innerText()).includes('不会补发') && !calls.some(call => call.path.startsWith('/api/legacy') && call.method !== 'GET'));
  await page.getByLabel('记录类型', { exact: true }).selectOption('undelivered');
  await page.getByRole('button', { name: '筛选档案', exact: true }).click();
  await page.getByText('找到 1 条，已展示 1 条。', { exact: true }).waitFor();
  check('archive type filtering uses the declared API kind', calls.some(call => call.path === '/api/legacy/history' && call.query.includes('kind=undelivered')) && await page.locator('.legacy-record').count() === 1);
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [name, suffix] of [['自选股票', 'watchlist'], ['综合行情', 'markets'], ['资讯与日历', 'news'], ['旧版历史档案', 'archive']]) {
      await page.getByRole('link', { name, exact: name !== '自选股票' }).click();
      await page.waitForTimeout(100);
      check(`${suffix} does not overflow at ${width}px`, await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
      if (width !== 320) await page.screenshot({ path: `${output}ui-${suffix}-${width}.png`, fullPage: true });
    }
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
  check('archive reflows under 200 percent CSS zoom', await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
  check('browser execution has no errors or external requests', errors.length === 0 && blockedExternal.length === 0);
  await writeFile(`${output}ui-contract-result.json`, JSON.stringify({ ok: true, scope: 'Isolated UI contract fixtures only; no real data persistence, market or delivery claim', checks, calls, errors, blockedExternal }, null, 2));
  console.log(JSON.stringify({ ok: true, checks: checks.length, output }));
} catch (error) {
  if (page) await page.screenshot({ path: `${output}ui-contract-failure.png`, fullPage: true }).catch(() => {});
  await writeFile(`${output}ui-contract-result.json`, JSON.stringify({ ok: false, checks, calls, errors, blockedExternal, error: String(error) }, null, 2));
  throw error;
} finally { if (browser) await browser.close(); await server.close(); }
