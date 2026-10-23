// Real browser -> API -> SQLite. API responses are never substituted.
// Identities, research and daily inputs are controlled; live price/model/notification calls are forbidden.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';

const { chromium } = process.env.ALPHA_UI_PLAYWRIGHT_MODULE
  ? await import(pathToFileURL(process.env.ALPHA_UI_PLAYWRIGHT_MODULE).href) : await import('playwright-core');
const root = fileURLToPath(new URL('../../', import.meta.url));
const runId = new Date().toISOString().replace(/[:.]/g, '-') + '-' + randomUUID().slice(0, 8);
const output = fileURLToPath(new URL(`../../.runtime/unified-flow/${runId}/`, import.meta.url));
const evidence = fileURLToPath(new URL('../../evidence/unification-implementation-20261023/', import.meta.url));
const python = process.env.ALPHA_UI_PYTHON || `${root}.venv/Scripts/python.exe`;
const checks = [], apiCalls = [], pageErrors = [], externalRequests = [], stages = [];
const check = (name, value) => { assert.ok(value, name); checks.push(name); };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let fixture, browser, page, mobile, ready, base;

async function start(resume = false) {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
  for (const key of ['CREDENTIALS_DIRECTORY', 'ALPHA_LAB_PUBLIC_URL', 'ALPHA_LAB_DB']) delete env[key];
  const args = ['-X', 'utf8', `${root}frontend/tests/unified-flow-fixture.py`, '--directory', output];
  if (resume) args.push('--resume', '--port', String(ready.port));
  const child = spawn(python, args, { cwd: root, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  let errors = '', terminalError;
  const queue = [], pending = [];
  child.stderr.on('data', value => { errors += value; });
  const rejectPending = error => { terminalError = error; while (pending.length) pending.shift().reject(error); };
  child.on('error', rejectPending);
  child.on('exit', code => rejectPending(new Error(`Fixture exited ${code}: ${errors}`)));
  const reader = createInterface({ input: child.stdout });
  reader.on('line', line => {
    try { const value = JSON.parse(line); if (pending.length) pending.shift().resolve(value); else queue.push(value); }
    catch { rejectPending(new Error(`Unexpected fixture output: ${line}`)); }
  });
  async function receive() {
    if (queue.length) return queue.shift();
    if (terminalError) throw terminalError;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Fixture response timed out: ${errors}`)), 20000);
      pending.push({ resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } });
    });
  }
  fixture = { child, receive, async command(value) { child.stdin.write(JSON.stringify(value) + '\n'); return receive(); } };
  ready = await receive(); assert.equal(ready.ready, true);
  base = `http://127.0.0.1:${ready.port}`;
}

async function stop() {
  if (!fixture) return;
  const current = fixture; fixture = null;
  if (current.child.exitCode !== null) return;
  const finished = once(current.child, 'exit');
  current.child.stdin.write('{"action":"stop"}\n');
  const timeout = setTimeout(() => current.child.kill(), 12000);
  try { const [code] = await finished; assert.equal(code, 0, 'fixture exits cleanly'); }
  finally { clearTimeout(timeout); }
}

async function read(path) {
  const response = await page.request.get(base + '/api' + path);
  assert.equal(response.status(), 200, path); return response.json();
}
async function stats(stage) {
  const value = await fixture.command({ action: 'stats' });
  if (stage) stages.push({ stage, ...value }); return value;
}
async function poll(fn, predicate, title) {
  const end = Date.now() + 12000;
  while (Date.now() < end) { const value = await fn(); if (predicate(value)) return value; await pause(100); }
  throw new Error('Timed out: ' + title);
}
async function stock(symbol) { return (await read('/watchlist')).items.find(item => item.symbol === symbol); }
async function pageFor(width) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    externalRequests.push(route.request().url()); return route.abort();
  });
  const tab = await context.newPage(); tab.setDefaultTimeout(12000);
  tab.on('pageerror', error => pageErrors.push(String(error)));
  tab.on('response', response => { const url = new URL(response.url()); if (url.pathname.startsWith('/api/')) apiCalls.push({ method: response.request().method(), path: url.pathname, status: response.status() }); });
  return tab;
}
async function addStock(symbol, name) {
  await page.getByLabel('股票代码', { exact: true }).fill(symbol);
  await page.getByRole('button', { name: '查找股票', exact: true }).click();
  await page.locator('.candidate').getByRole('heading', { name, exact: true }).waitFor();
  await page.getByRole('button', { name: '加入自选', exact: true }).click();
  await page.getByLabel(`${name}分组`, { exact: true }).waitFor();
  await poll(() => stock(symbol), Boolean, 'stock saved');
}

try {
  await start();
  browser = await chromium.launch({ headless: true }); page = await pageFor(1280);
  await page.goto(base, { waitUntil: 'networkidle' });
  const initial = await stats('fresh');
  check('fresh private database contains only the controlled archive and no active messages', initial.counts.watchlist === 0 && initial.counts.legacy_records === 34 && initial.counts.notification_messages === 0 && initial.research_calls.length === 0);
  // New stocks are prepended by the real store, so add A last before moving it down.
  await addStock('000001', '集成股票乙'); await addStock('600519', '集成股票甲');
  check('browser identity lookup and add use the actual persistent API', (await read('/watchlist')).total === 2);
  await page.locator('.watch-groups > summary').click();
  await page.getByLabel('新分组名称', { exact: true }).fill('长期关注');
  await page.getByRole('button', { name: '创建分组', exact: true }).click();
  const group = (await poll(() => read('/watchlist/groups'), result => result.items.length === 1, 'group saved')).items[0];
  await page.getByLabel('集成股票甲分组', { exact: true }).selectOption(group.id);
  await page.getByText('集成股票甲的分组已保存。', { exact: true }).waitFor();
  await page.getByRole('checkbox', { name: '标记集成股票甲为持仓', exact: true }).check();
  await page.getByText('集成股票甲的持仓标记已保存。', { exact: true }).waitFor();
  const managed = await stock('600519');
  check('group and holding are saved together with real metadata versions', managed.group_id === group.id && managed.is_holding && managed.metadata_version === 2);
  mobile = await pageFor(390); await mobile.goto(base, { waitUntil: 'networkidle' });
  check('a separate mobile browser context reads the same group and holding', await mobile.getByLabel('集成股票甲分组', { exact: true }).inputValue() === group.id && await mobile.getByRole('checkbox', { name: '标记集成股票甲为持仓', exact: true }).isChecked());
  await page.getByRole('button', { name: '下移集成股票甲', exact: true }).click();
  await page.getByText('已下移集成股票甲，顺序已保存。', { exact: true }).waitFor();
  check('full ordering is saved by the actual store', (await read('/watchlist')).items[0].symbol === '000001');
  await page.reload({ waitUntil: 'networkidle' });
  const concurrent = await stock('000001');
  const otherWrite = await mobile.request.patch(base + '/api/watchlist/sz/000001', { headers: { 'X-Alpha-Request': 'watchlist-v1' }, data: { watchlist_created_at: concurrent.created_at, expected_version: concurrent.metadata_version, group_id: concurrent.group_id, is_holding: true } });
  assert.equal(otherWrite.status(), 200);
  await page.getByRole('button', { name: '上移集成股票甲', exact: true }).click();
  await page.getByText('顺序未保存：', { exact: false }).waitFor();
  check('a second browser write makes stale ordering fail without overwriting it', apiCalls.some(call => call.path === '/api/watchlist/reorder' && call.status === 409) && (await read('/watchlist')).items[0].symbol === '000001');
  await page.locator('.watch-groups > summary').click();
  await page.getByRole('button', { name: '重命名长期关注分组', exact: true }).click();
  await page.getByLabel('修改分组名称', { exact: true }).fill('重点跟踪');
  await page.getByRole('button', { name: '保存名称', exact: true }).click();
  await poll(() => read('/watchlist/groups'), result => result.items[0].name === '重点跟踪', 'renamed group');
  check('renaming a group keeps its stable assignment', (await stock('600519')).group_id === group.id);

  await page.getByRole('button', { name: '设置集成股票甲提醒', exact: true }).click();
  await page.locator('.anomaly-rule').waitFor();
  const rule = page.locator('.anomaly-rule');
  await rule.getByLabel('上涨达到（%）', { exact: true }).fill('4');
  await rule.getByRole('checkbox', { name: '启用真实行情规则', exact: false }).check();
  await rule.getByRole('button', { name: '保存规则', exact: true }).click();
  await rule.getByText('规则已保存并开启。', { exact: false }).waitFor();
  await page.locator('.extended-rule > summary').click();
  const extended = page.locator('.extended-rule');
  const beforeExtended = await read('/anomalies/extended/sh/600519');
  check('actual extended settings start fully disabled', !beforeExtended.enabled && Object.entries(beforeExtended.config).filter(([key]) => key.endsWith('_enabled')).every(([, enabled]) => !enabled));
  await extended.getByRole('checkbox', { name: '启用扩展提醒', exact: false }).check();
  await extended.getByRole('checkbox', { name: '振幅异常', exact: false }).check();
  await extended.getByRole('checkbox', { name: '量比异常', exact: false }).check();
  await extended.getByLabel('振幅阈值（%）', { exact: true }).fill('9');
  await extended.getByLabel('量比阈值（倍）', { exact: true }).fill('4.2');
  await extended.getByLabel('历史对照天数', { exact: true }).fill('25');
  check('editing choices does not persist until explicit save', !(await read('/anomalies/extended/sh/600519')).enabled);
  await extended.getByRole('button', { name: '保存扩展提醒', exact: true }).click();
  await extended.getByText('扩展提醒设置已保存。', { exact: false }).waitFor();
  const savedExtended = await read('/anomalies/extended/sh/600519');
  check('explicit extended save persists exact per-type choices and thresholds', savedExtended.enabled && savedExtended.config.amplitude_enabled && savedExtended.config.volume_enabled && savedExtended.config.volume_ratio === '4.2' && savedExtended.config.lookback_days === 25 && savedExtended.version === 1);
  await page.getByRole('button', { name: '收起异动检测', exact: true }).click();
  const oldGeneration = (await stock('600519')).created_at;
  await page.getByRole('button', { name: '移除集成股票甲', exact: true }).click();
  await page.getByRole('button', { name: '确认移除集成股票甲', exact: true }).click();
  await page.getByRole('button', { name: '撤销移除', exact: true }).waitFor();
  check('actual deletion removes the active generation and offers timed undo', (await read('/watchlist')).total === 1);
  await page.getByRole('button', { name: '撤销移除', exact: true }).click();
  await page.getByText('原有规则设置已恢复为关闭', { exact: false }).waitFor();
  const restored = await stock('600519'), restoredExtended = await read('/anomalies/extended/sh/600519');
  const restoredBase = (await read('/anomalies')).items.find(item => item.symbol === '600519').rule;
  check('undo restores group and holding under a new generation', restored.created_at !== oldGeneration && restored.group_id === group.id && restored.is_holding);
  check('undo preserves both rule configurations but keeps both overall switches off', !restoredBase.enabled && restoredBase.up_threshold === '4' && !restoredExtended.enabled && JSON.stringify(restoredExtended.config) === JSON.stringify(savedExtended.config));
  await mobile.reload({ waitUntil: 'networkidle' });
  check('the restored state is visible to the other device', await mobile.getByRole('checkbox', { name: '标记集成股票甲为持仓', exact: true }).isChecked() && await mobile.getByLabel('集成股票甲分组', { exact: true }).inputValue() === group.id);
  await mobile.screenshot({ path: output + 'management-390.png', fullPage: true });
  check('real management screen has no horizontal overflow on mobile', await mobile.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));

  await page.getByRole('link', { name: '综合行情', exact: true }).click();
  await page.locator('.research-panel').waitFor();
  check('research reads do not acquire sources', (await stats()).research_calls.length === 0);
  await page.getByRole('button', { name: '更新资料', exact: true }).click();
  await page.getByRole('link', { name: '受控indices指标', exact: false }).waitFor();
  const firstResearch = await read('/research'), firstNews = firstResearch.sections.find(section => section.key === 'news').items[0];
  check('explicit research update creates all six real persisted sections', firstResearch.sections.length === 6 && firstResearch.sections.every(section => section.items.length === 1) && (await stats()).research_calls.length === 6);
  const cooldown = await page.request.post(base + '/api/research/refresh', { headers: { 'X-Alpha-Request': 'watchlist-v1' } });
  assert.equal(cooldown.status(), 202);
  check('server cooldown does not duplicate provider work', (await stats()).research_calls.length === 6);
  await fixture.command({ action: 'source', revision: 2, advance: 61 });
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '更新资料', exact: true }).click();
  await poll(() => read('/research'), value => value.snapshot_id !== firstResearch.snapshot_id && value.refresh_state !== 'processing', 'updated research snapshot');
  await page.getByRole('link', { name: '资讯与日历', exact: true }).click();
  await page.getByText('合成数值 200 万元，不代表真实资讯。', { exact: true }).first().waitFor();
  const secondResearch = await read('/research'), secondNews = secondResearch.sections.find(section => section.key === 'news').items[0];
  check('a numeric change keeps the stable article id and changes its saved fingerprint', secondNews.id === firstNews.id && secondNews.content_fingerprint !== firstNews.content_fingerprint && secondNews.change_kind === 'updated');
  check('the actual news view shows content changes and separate source times', await page.getByText('有更新', { exact: true }).count() >= 1 && (await page.locator('.research-panel').innerText()).includes('发布时间：') && (await page.locator('.research-panel').innerText()).includes('抓取时间：'));
  await fixture.command({ action: 'source', fail: true, advance: 61 });
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '更新资料', exact: true }).click();
  const failedResearch = await poll(() => read('/research'), value => value.refresh_state === 'failed', 'failed upstream refresh');
  await poll(() => page.locator('.research-panel').innerText(), text => text.includes('上次资料') || text.includes('来源未能'), 'retained data UI');
  check('a real upstream failure retains saved news in the API and browser', failedResearch.sections.find(section => section.key === 'news').items[0].summary === secondNews.summary && (await page.locator('.research-panel').innerText()).includes(secondNews.summary));
  await fixture.command({ action: 'source', fail: false, revision: 2, advance: 61 });
  await page.getByRole('link', { name: '自选股票', exact: false }).click();
  await page.getByRole('button', { name: '查看集成股票甲资讯与公告', exact: true }).click();
  const beforeScoped = (await stats()).research_calls.length;
  await page.locator('.stock-research').getByRole('button', { name: '更新资料', exact: true }).click();
  await page.locator('.stock-research').getByText(secondNews.summary, { exact: true }).first().waitFor();
  const scoped = await read('/research/sh/600519');
  check('stock-scoped refresh saves its own snapshot through the shared service', scoped.scope === 'sh:600519' && scoped.snapshot_id !== failedResearch.snapshot_id && (await stats()).research_calls.length > beforeScoped);
  await page.locator('.stock-research').getByRole('button', { name: '收起资料', exact: true }).click();

  // Respect the shared two-second provider cooldown before the distinct briefing refresh.
  await fixture.command({ action: 'source', fail: false, revision: 2, advance: 3 });
  const beforeBriefing = await stats();
  await page.getByRole('button', { name: '早盘简报', exact: false }).click();
  const briefing = page.locator('#briefing-details');
  await briefing.getByRole('button', { name: '手动生成今日摘要', exact: true }).waitFor();
  check('opening the real briefing panel only reads saved state', (await read('/briefings')).reports.length === 0 && (await stats()).daily_calls.length === 0 && (await stats()).research_calls.length === beforeBriefing.research_calls.length);
  await briefing.getByRole('button', { name: '手动生成今日摘要', exact: true }).click();
  await poll(() => read('/briefings'), value => value.reports.length === 1, 'briefing queued by UI');
  // The fixture disables all real schedulers. Run the registered service's worker
  // method via its private stdin control after the actual browser request queues it.
  await fixture.command({ action: 'briefing' });
  await briefing.getByText('摘要已保存 · 有缺失项', { exact: true }).waitFor();
  const report = (await read('/briefings')).reports[0];
  check('actual briefing processing saves known daily data and an explicit missing item', report.status === 'partial' && report.output.coverage.covered === 1 && report.output.coverage.missing === 1 && (await briefing.innerText()).includes('合成来源本项不可用。'));
  await briefing.getByText('展开全部市场与资讯资料', { exact: true }).click();
  check('the saved briefing expands all six frozen research sections', await briefing.locator('.research-sections .research-section').count() === 6 && report.output.research_context.sections.length === 6 && report.output.research_coverage.available_sections.length === 6 && report.output.research_evidence.length === 5);
  check('manual briefing remains local with zero model fees and no notification queue', report.delivery_status === 'local_only' && report.notification === null && report.output.model_cost === '0' && (await stats()).counts.notification_messages === 0 && (await stats()).counts.ai_explanations === 0);
  await briefing.getByText('查看整理与生成过程', { exact: true }).click();
  check('briefing displays generation trace and exact research cutoff', (await briefing.innerText()).includes('本次未使用') && (await briefing.innerText()).includes('资料截止：'));
  await page.setViewportSize({ width: 390, height: 900 });
  check('expanded actual briefing reflows on mobile', await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
  await briefing.screenshot({ path: output + 'briefing-390.png' });
  await page.setViewportSize({ width: 1280, height: 900 });
  await briefing.locator('.briefing-research').screenshot({ path: output + 'briefing-research-desktop.png' });
  const callsAfterBriefing = await stats();
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '早盘简报', exact: false }).click();
  await briefing.getByText('摘要已保存 · 有缺失项', { exact: true }).waitFor();
  await briefing.getByText('展开全部市场与资讯资料', { exact: true }).click();
  check('refresh and reopening keep the same briefing and source records without refetch', JSON.stringify((await read('/briefings')).reports[0].output) === JSON.stringify(report.output) && (await stats()).research_calls.length === callsAfterBriefing.research_calls.length && (await stats()).daily_calls.length === callsAfterBriefing.daily_calls.length);

  await page.getByRole('link', { name: '旧版历史档案', exact: true }).click();
  await page.locator('.legacy-record').first().waitFor();
  check('actual archive first page is bounded and delivery is not human receipt', await page.locator('.legacy-record').count() === 30 && await page.getByText(/未确认本人实收/).count() > 0);
  await page.getByRole('button', { name: '读取更多档案', exact: true }).click();
  await poll(() => page.locator('.legacy-record').count(), value => value === 34, 'archive pagination');
  check('actual cursor pagination displays all records without duplicates', await page.locator('.legacy-record').count() === 34 && (await read('/legacy/history?limit=100')).total === 34);
  await page.getByLabel('记录类型', { exact: true }).selectOption('undelivered');
  await page.getByRole('button', { name: '筛选档案', exact: true }).click();
  await page.getByText('这条旧队列仅供归档回查，不会补发。', { exact: true }).waitFor();
  check('filtered old unsent queue remains a read-only archive', await page.locator('.legacy-record').count() === 1 && (await stats()).counts.notification_messages === 0);
  const beforeRestart = { items: (await read('/watchlist')).items, groups: (await read('/watchlist/groups')).items, extended: await read('/anomalies/extended/sh/600519'), research: await read('/research/sh/600519'), migration: await read('/legacy/migration'), briefing: (await read('/briefings')).reports[0] };
  await stats('before-restart');
  const firstPid = ready.pid;
  await page.goto('about:blank'); await mobile.goto('about:blank'); await stop(); await start(true);
  await page.goto(base, { waitUntil: 'networkidle' });
  check('the integrated app restarts in a new process', ready.pid !== firstPid);
  const afterRestart = { items: (await read('/watchlist')).items, groups: (await read('/watchlist/groups')).items, extended: await read('/anomalies/extended/sh/600519'), research: await read('/research/sh/600519'), migration: await read('/legacy/migration'), briefing: (await read('/briefings')).reports[0] };
  check('restart preserves exact management, rule, research, briefing and migration records', JSON.stringify(afterRestart) === JSON.stringify(beforeRestart));
  await page.getByRole('button', { name: '早盘简报', exact: false }).click();
  await briefing.getByText('摘要已保存 · 有缺失项', { exact: true }).waitFor();
  await briefing.getByText('展开全部市场与资讯资料', { exact: true }).click();
  check('the same saved briefing remains readable after process restart', await briefing.locator('.research-sections .research-section').count() === 6 && (await read('/briefings')).reports[0].id === report.id);
  const restarted = await stats('restarted');
  check('restart and read paths neither refresh sources nor send or generate', restarted.research_calls.length === 0 && restarted.daily_calls.length === 0 && restarted.counts.briefing_reports === 1 && restarted.counts.notification_messages === 0 && restarted.counts.ai_explanations === 0 && restarted.counts.anomaly_events === 0);
  await page.locator('.watch-groups > summary').click();
  await page.getByRole('button', { name: '删除重点跟踪分组', exact: true }).click();
  await page.getByRole('button', { name: '删除分组并保留股票', exact: true }).click();
  await page.getByText('已删除“重点跟踪”分组，股票仍保留在未分组中。', { exact: true }).waitFor();
  check('deleting a group retains its stocks and holding marks', (await read('/watchlist/groups')).items.length === 0 && (await read('/watchlist')).total === 2 && (await stock('600519')).group_id === null && (await stock('600519')).is_holding);
  const final = await stats('final');
  check('SQLite integrity and external-call isolation hold across every stage', stages.every(value => value.integrity === 'ok' && value.foreign_keys.length === 0 && value.blocked_network.length === 0 && value.forbidden_calls.length === 0) && final.counts.notification_messages === 0 && externalRequests.length === 0);
  check('browser execution has no errors or unexpected failed APIs', pageErrors.length === 0 && apiCalls.every(call => call.status < 400 || call.path === '/api/watchlist/reorder' && call.status === 409));
  const result = { ok: true, scope: 'Real built frontend, API, SQLite, two browser contexts and process restart; controlled research, daily and identity inputs; real briefing worker method explicitly invoked after UI queues; no production, natural-schedule, natural-market, real-model or human-receipt assertion', output, checks, stages, apiCalls, pageErrors, externalRequests };
  await mkdir(evidence, { recursive: true });
  await writeFile(output + 'result.json', JSON.stringify(result, null, 2));
  await writeFile(evidence + 'ui-integrated-result.json', JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ ok: true, checks: checks.length, output }));
} catch (error) {
  await mkdir(output, { recursive: true });
  if (page && !page.isClosed()) await page.screenshot({ path: output + 'failure.png', fullPage: true }).catch(() => {});
  await writeFile(output + 'result.json', JSON.stringify({ ok: false, checks, stages, apiCalls, pageErrors, externalRequests, error: String(error) }, null, 2));
  console.error(JSON.stringify({ ok: false, output, error: String(error) })); process.exitCode = 1;
} finally { if (browser) await browser.close(); await stop(); }
