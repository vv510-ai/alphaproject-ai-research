// Browser -> actual application -> real SQLite. No API response is intercepted.
// Only external market/model/Feishu inputs are simulated in the Python fixture.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';

const { chromium } = process.env.ALPHA_UI_PLAYWRIGHT_MODULE
  ? await import(pathToFileURL(process.env.ALPHA_UI_PLAYWRIGHT_MODULE).href)
  : await import('playwright-core');
const root = fileURLToPath(new URL('../../', import.meta.url));
const runId = new Date().toISOString().replace(/[:.]/g, '-') + '-' + randomUUID().slice(0, 8);
const output = fileURLToPath(new URL(`../../.runtime/persistent-flow/${runId}/`, import.meta.url));
const python = process.env.ALPHA_UI_PYTHON || `${root}.venv/Scripts/python.exe`;
const checks = [], apiCalls = [], pageErrors = [], externalRequests = [], stages = [];
const check = (name, value) => { assert.ok(value, name); checks.push(name); };
let fixture, browser, page, ready, base;

async function start(resume = false) {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
  for (const key of ['CREDENTIALS_DIRECTORY', 'ALPHA_LAB_PUBLIC_URL', 'ALPHA_LAB_DB']) delete env[key];
  const args = ['-X', 'utf8', `${root}backend/tests/browser_flow_fixture.py`, '--directory', output];
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
    try {
      const value = JSON.parse(line);
      if (pending.length) pending.shift().resolve(value); else queue.push(value);
    } catch { rejectPending(new Error(`Unexpected fixture output: ${line}`)); }
  });
  async function receive() {
    if (queue.length) return queue.shift();
    if (terminalError) throw terminalError;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Fixture response timed out: ${errors}`)), 20000);
      pending.push({ resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } });
    });
  }
  fixture = { child, receive, async stats() { child.stdin.write('{"action":"stats"}\n'); return receive(); } };
  const next = await receive();
  assert.equal(next.ready, true);
  if (ready) assert.equal(next.event_id, ready.event_id);
  ready = next;
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
  assert.equal(response.status(), 200, path);
  return response.json();
}

async function openNotifications() {
  const button = page.getByRole('button', { name: '飞书通知', exact: false }).first();
  if (await button.getAttribute('aria-expanded') !== 'true') await button.click();
  await page.locator('#notification-details .notification-records li').waitFor();
}

try {
  await start();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    externalRequests.push(route.request().url()); return route.abort();
  });
  page = await context.newPage();
  page.setDefaultTimeout(12000);
  page.on('pageerror', error => pageErrors.push(String(error)));
  page.on('response', response => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/')) apiCalls.push({ method: response.request().method(), path: url.pathname, status: response.status() });
  });
  await page.goto(`${base}/#event=${ready.event_id}`, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '用这些依据生成解释', exact: true }).waitFor();
  const route = `/events/${ready.event_id}/explanations`;
  const original = await read(route);
  check('real live event evidence loads through the browser without 422', original.evidence.mode === 'live' && original.records.length === 0);
  stages.push({ stage: 'seeded', ...await fixture.stats() });
  check('fixture is a new database with two controlled quotes and one simulated notification', stages[0].quote_fetches === 2 && stages[0].simulated_messages === 1 && stages[0].model_calls === 0);

  await page.getByRole('button', { name: '用这些依据生成解释', exact: true }).click();
  await page.getByText('生成未完成', { exact: true }).waitFor();
  check('model failure retains the accepted base notification and unconfirmed receipt', (await read('/notifications')).records[0].status === 'accepted' && !(await read('/notifications')).records[0].confirmed_at);
  await page.getByRole('button', { name: '重试一次（会再次调用模型）', exact: true }).click();
  await page.getByText('AI 解读已保存', { exact: true }).waitFor();
  const review = page.locator('.explanation-review');
  const save = review.getByRole('button', { name: '保存我的核对', exact: true });
  const boxes = review.getByRole('checkbox');
  await boxes.nth(0).check(); await boxes.nth(1).check();
  check('incomplete review cannot be saved', await save.isDisabled());
  await boxes.nth(2).check();
  check('checking every box alone does not persist a review', (await read(route)).records[0].reviewed_at === null);
  await page.reload({ waitUntil: 'networkidle' });
  await boxes.nth(0).waitFor();
  check('refresh discards unsaved review checkboxes', !await boxes.nth(0).isChecked() && !await boxes.nth(1).isChecked() && !await boxes.nth(2).isChecked() && await save.isDisabled());
  for (let index = 0; index < 3; index++) await boxes.nth(index).check();
  await save.click();
  await review.getByText(/你已在.*保存核对/).waitFor();
  const afterReview = await read(route);
  check('explicit review click saves the selected successful record', !!afterReview.records[0].reviewed_at && afterReview.records.length === 2 && afterReview.records[1].reviewed_at === null);

  await openNotifications();
  const receipt = page.locator(`#receipt-${ready.notification_id}`);
  await receipt.fill(ready.receipt_code === '000000' ? '111111' : '000000');
  await page.getByRole('button', { name: '确认我已收到', exact: true }).click();
  await page.getByText('核验码不匹配，请核对飞书里相同记录标识的消息。', { exact: false }).waitFor();
  check('wrong code does not save a receipt', (await read('/notifications')).records[0].confirmed_at === null);
  await receipt.fill(ready.receipt_code);
  await page.getByRole('button', { name: '确认我已收到', exact: true }).click();
  await page.locator('#notification-details').getByText('你已确认收到', { exact: true }).waitFor();
  const saved = await fixture.stats(); stages.push({ stage: 'saved', ...saved });
  check('actual SQLite stores both confirmations and the failed attempt', !!saved.notification.confirmed_at && saved.notification.confirmation_attempts === 2 && saved.records.filter(row => row.reviewed_at).length === 1);
  check('only an explicit retry called the simulated model again', saved.model_calls === 2 && saved.simulated_messages === 1);
  const savedState = await read(route);
  await page.locator('#notification-details').screenshot({ path: output + 'notification-desktop.png' });
  await page.locator('.evidence-entry').screenshot({ path: output + 'evidence-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  check('saved evidence and receipt have no horizontal overflow at 390px', await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
  await page.locator('.evidence-entry').screenshot({ path: output + 'evidence-390.png' });
  await page.reload({ waitUntil: 'networkidle' });
  await review.getByText(/你已在.*保存核对/).waitFor();
  await openNotifications();
  await page.locator('#notification-details').getByText('你已确认收到', { exact: true }).waitFor();
  check('refresh retains exact saved records and budget', JSON.stringify((await read(route)).records) === JSON.stringify(savedState.records) && (await read(route)).budget.month_used === savedState.budget.month_used);
  check('refresh does not call either external substitute', (await fixture.stats()).model_calls === 2 && (await fixture.stats()).simulated_messages === 1);

  await page.goto('about:blank');
  const firstPid = ready.pid;
  await stop(); await start(true);
  check('the application restarts in a different process', ready.pid !== firstPid);
  await page.goto(`${base}/#event=${ready.event_id}`, { waitUntil: 'networkidle' });
  await review.getByText(/你已在.*保存核对/).waitFor();
  await openNotifications();
  await page.locator('#notification-details').getByText('你已确认收到', { exact: true }).waitFor();
  const restart = await fixture.stats(); stages.push({ stage: 'restarted_next_day', ...restart });
  check('next-day process restart preserves confirmations without another call or send', restart.model_calls === 0 && restart.simulated_messages === 0 && restart.notification.confirmed_at === saved.notification.confirmed_at && JSON.stringify(restart.records) === JSON.stringify(saved.records));
  check('old event evidence remains identical after restart and clock change', (await read(route)).evidence.evidence_hash === original.evidence.evidence_hash);
  await page.getByRole('button', { name: '移除合成验收股票', exact: true }).click();
  await page.getByRole('button', { name: '确认移除合成验收股票', exact: true }).click();
  await page.getByRole('heading', { name: '从第一只关注的股票开始', exact: true }).waitFor();
  check('UI deletion removes the active stock but keeps the same historical explanation', (await read('/watchlist')).total === 0 && (await read(route)).records[0].id === savedState.records[0].id);
  await page.goto('about:blank'); await stop(); await start(true);
  await page.goto(`${base}/#event=${ready.event_id}`, { waitUntil: 'networkidle' });
  await review.getByText(/你已在.*保存核对/).waitFor();
  const final = await fixture.stats(); stages.push({ stage: 'deleted_and_restarted', ...final });
  check('deletion and historical confirmations survive another process restart', final.watchlist_count === 0 && final.notification.confirmed_at === saved.notification.confirmed_at && JSON.stringify(final.records) === JSON.stringify(saved.records));
  check('database integrity and all isolation guards hold', stages.every(row => row.integrity === 'ok' && row.blocked_network.length === 0) && externalRequests.length === 0);
  check('the browser had no runtime errors or unexpected failing API responses', pageErrors.length === 0 && apiCalls.every(row => row.status < 400 || row.path === `/api/notifications/${ready.notification_id}/confirm` && row.status === 400));
  await writeFile(output + 'result.json', JSON.stringify({ ok: true, scope: 'Actual frontend, API, services, SQLite and process restart; synthetic live inputs, simulated Feishu/model; no natural-market or human-receipt assertion', checks, stages, apiCalls, pageErrors, externalRequests }, null, 2));
  console.log(JSON.stringify({ ok: true, checks: checks.length, output }));
} catch (error) {
  await mkdir(output, { recursive: true });
  if (page && !page.isClosed()) await page.screenshot({ path: output + 'failure.png', fullPage: true }).catch(() => {});
  await writeFile(output + 'result.json', JSON.stringify({ ok: false, checks, stages, apiCalls, pageErrors, externalRequests, error: String(error) }, null, 2));
  console.error(JSON.stringify({ ok: false, output, error: String(error) }));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  await stop();
}
