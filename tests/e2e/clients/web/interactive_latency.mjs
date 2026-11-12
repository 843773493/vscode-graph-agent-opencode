import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { Perf } from '../../../harness/js/performance.mjs';
import { observePage } from '../../../harness/js/browser-performance.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!baseUrl || !resultPath) throw new Error('延迟验收缺少隔离 Gateway 或产物目录');
const run = TestRunContext.fromTestFile('tests/e2e/clients/web/test_interactive_latency.py');
const perf = new Perf(run);
const result = { classification: 'E2E', phase: 'startup', pageErrors: [], forbiddenExecutions: [],
  samples: [], navigationMs: [], sessionOpenMs: [], fileOpenMs: [],
  boundary: '真实 Workbench/Gateway/Backend；无模型请求、HTTP 替身、浏览器缓存预写；目录32会话/128文件，非深历史验收' };
let page, failure, token, route;
const observations = [];
const pending = new Map();

function summary(values) {
  const ordered = [...values].sort((a, b) => a - b);
  return { count: values.length, p50: ordered[Math.ceil(values.length * 0.5) - 1],
    p95: ordered[Math.ceil(values.length * 0.95) - 1], max: ordered.at(-1) };
}

async function api(path, data) {
  const receipt = await page.evaluate(async ({ path, data, token, route }) => {
    // 从发起到完整正文消费：校验、队列、Gateway和Backend全算，不能只计服务helper。
    const started = performance.now();
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': token, 'Content-Type': 'application/json',
        ...(route ? { 'X-BoxTeam-Target-Gateway-Id': route.gateway_id, 'X-BoxTeam-Workspace-Id': route.workspace_id } : {}) },
      body: data === undefined ? undefined : JSON.stringify(data) });
    const text = await response.text(), body = JSON.parse(text);
    if (!response.ok || !body.request_id || body.request_id !== response.headers.get('x-request-id')) {
      throw new Error(JSON.stringify({ path, status: response.status, body }));
    }
    return { body: body.data, elapsedMs: performance.now() - started,
      bytes: new TextEncoder().encode(text).length, requestId: body.request_id, path };
  }, { path, data, token, route });
  result.samples.push({ ...receipt, body: undefined, phase: result.phase });
  perf.record('end', 'http.browser_api', { stage_id: crypto.randomUUID(), elapsed_ns: Math.round(receipt.elapsedMs * 1e6),
    status: 'passed', request_id: receipt.requestId, response_bytes: receipt.bytes, inclusive: true }, { phase: result.phase });
  return receipt;
}

const command = (id, ...args) => page.evaluate(
  ({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  const observer = await observePage(page, perf, context);
  if (observer.close) run.addCleanup('页面性能观察', () => observer.close());
  // Chromium网络事件证明实际传输；不把合成响应或DOM可见冒充完整正文传输。
  const cdp = await context.newCDPSession(page), transfers = new Map();
  cdp.on('Network.responseReceived', event => {
    if (new URL(event.response.url).pathname !== '/api/v1/workspace/files/raw') return;
    transfers.set(event.requestId, { url: event.response.url, status: event.response.status,
      headers: event.response.headers, bytes: 0, finished: false });
  });
  // Blob读取在本机Chromium的dataLength可为0；encodedDataLength才是实际正文传输量。
  cdp.on('Network.dataReceived', event => {
    const transfer = transfers.get(event.requestId);
    if (transfer) transfer.bytes += event.encodedDataLength;
  });
  cdp.on('Network.loadingFinished', event => {
    const transfer = transfers.get(event.requestId);
    if (transfer) transfer.finished = true;
  });
  await cdp.send('Network.enable');
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('requestfinished', request => pending.delete(request));
  page.on('requestfailed', request => {
    pending.delete(request);
    observations.push({ kind: 'request-failed', url: request.url(), error: request.failure() });
  });
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) pending.set(request, performance.now());
    if (request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(new URL(request.url()).pathname)) {
      result.forbiddenExecutions.push(request.url());
    }
  });
  // 进程/静态资源启动独立于已就绪后的交互预算，原始启动耗时仍要报告。
  const startup = performance.now();
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  result.startupMs = performance.now() - startup;
  token = await page.evaluate(async () => (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token);
  const health = (await api('/api/gateway/health')).body;
  route = (await api('/api/gateway/workspaces')).body.active_workspace;
  expect(route.gateway_id).toBe(health.gateway_id);
  expect(route.workspace_id).toBeTruthy();
  const workspace = (await api('/api/v1/workspace')).body;
  expect(workspace.workspace_id).toBe(route.workspace_id);
  const roots = (await api('/api/v1/workspace/files/roots')).body;
  result.owner = { gatewayId: health.gateway_id, workspaceId: workspace.workspace_id, route };
  result.phase = 'prepare-catalog';
  const sessions = [];
  for (let index = 0; index < 32; index++) {
    const created = (await api('/api/v1/sessions', { title: `latency-session-${String(index).padStart(2, '0')}` })).body;
    if (index < 2) {
      const main = (await api(`/api/v1/sessions/${created.session_id}/main-thread`)).body;
      sessions.push({ sessionId: created.session_id, threadId: main.thread_id });
    }
  }

  result.phase = 'first-data-read';
  const cold = await api('/api/v1/session-catalog/export');
  result.firstReadMs = cold.elapsedMs;
  expect(cold.body.items.length).toBeGreaterThanOrEqual(32);
  for (const session of sessions) expect(cold.body.items.some(item => item.node_id === session.sessionId)).toBe(true);
  expect(cold.elapsedMs, '首次目录数据读取总预算').toBeLessThanOrEqual(3000);

  result.phase = 'warm-navigation';
  for (let iteration = 0; iteration < 20; iteration++) {
    const started = performance.now();
    const listing = await api('/api/gateway/workspaces');
    const current = await api('/api/v1/workspace');
    const catalog = await api('/api/v1/session-catalog/export');
    result.navigationMs.push(performance.now() - started);
    expect(listing.body.active_workspace).toEqual(route);
    expect(current.body.workspace_id).toBe(workspace.workspace_id);
    expect(catalog.body.items.length).toBeGreaterThanOrEqual(32);
  }
  result.navigation = summary(result.navigationMs);
  result.api = {};
  for (const path of ['/api/gateway/workspaces', '/api/v1/workspace', '/api/v1/session-catalog/export']) {
    const samples = result.samples.filter(item => item.phase === 'warm-navigation' && item.path === path);
    result.api[path] = summary(samples.map(item => item.elapsedMs));
    expect(result.api[path].p95, `${path} p95总处理时间`).toBeLessThanOrEqual(500);
    expect(result.api[path].max, `${path} 最慢一次`).toBeLessThanOrEqual(1500);
    expect(Math.max(...samples.map(item => item.bytes))).toBeLessThanOrEqual(512 * 1024);
  }
  expect(result.navigation.p95).toBeLessThanOrEqual(1000);
  expect(result.navigation.max).toBeLessThanOrEqual(2000);

  // 记录既定样本后按最慢一次判定，首开慢样本也保留，不重跑挑最好结果。
  result.phase = 'file-editor-open';
  for (let index = 0; index < 6; index++) {
    await command('workbench.action.closeAllEditors');
    const name = `latency-${String(index).padStart(3, '0')}.txt`;
    const uri = await page.evaluate(({ gatewayId, workspaceId, root, name }) =>
      globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: gatewayId,
        path: `/${workspaceId}/${encodeURIComponent(root)}/${name}` }).toString(),
    { gatewayId: health.gateway_id, workspaceId: workspace.workspace_id, root: roots.workspace_uri, name });
    const started = performance.now();
    const [response] = await Promise.all([
      page.waitForResponse(response => response.request().method() === 'GET' && new URL(response.url()).pathname === '/api/v1/workspace/files/raw'
        && new URL(response.url()).searchParams.get('path')?.endsWith('/' + name), { timeout: 15_000 }),
      command('_workbench.open', uri, [undefined, { pinned: true }]),
    ]);
    await expect(page.locator('.editor-group-container.active .view-lines')).toContainText(`LATENCY FILE ${String(index).padStart(3, '0')}`);
    let transfer;
    await expect.poll(() => {
      transfer = [...transfers.values()].find(item => Object.entries(item.headers).some(([key, value]) =>
        key.toLowerCase() === 'x-request-id' && value === response.headers()['x-request-id']));
      return transfer?.finished;
    }, { timeout: 15_000 }).toBe(true);
    const bytes = transfer.bytes;
    result.transfers = [...transfers.values()];
    const elapsed = performance.now() - started;
    result.fileOpenMs.push(elapsed);
    expect(response.status()).toBe(200);
    expect(transfer.status).toBe(200);
    expect(response.headers()['x-request-id']).toBeTruthy();
    expect(bytes).toBeGreaterThanOrEqual(128 * 1024);
    result.samples.push({ phase: result.phase, path: new URL(response.url()).pathname, bytes,
      requestId: response.headers()['x-request-id'], elapsedMs: elapsed });
    perf.record('end', 'ui.file.open', { stage_id: crypto.randomUUID(), elapsed_ns: Math.round(elapsed * 1e6), status: 'passed', inclusive: true },
      { bytes, cache_state: index === 0 ? 'first_explicit_open' : 'subsequent_open' });
    await observer.checkpoint('file.open', index);

  }
  expect(Math.max(...result.fileOpenMs), '每次文件打开到真实正文可见的总预算').toBeLessThanOrEqual(2000);
  result.phase = 'session-editor-open';
  const quick = page.locator('.quick-input-widget');
  for (let index = 0; index < 6; index++) {
    await command('workbench.action.closeAllEditors');
    const owner = sessions[index % 2];
    await command('workbench.action.quickOpen', `latency-session-0${index % 2}`);
    const pick = quick.locator(`[data-quick-input-id="session:${health.gateway_id}/${workspace.workspace_id}/${owner.sessionId}"]`);
    await expect(pick).toHaveCount(1);
    const started = performance.now();
    // 15秒只用于保留慢操作的原始结果；成功仍须满足下面的2秒断言。
    // 并行等待必须统一收敛，防止失败清理时未处理的Promise覆盖真正失败原因。
    const [response] = await Promise.all([page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === `/api/v1/sessions/${owner.sessionId}/bootstrap`
        && url.searchParams.get('thread_id') === owner.threadId;
    }, { timeout: 15_000 }), pick.click()]);
    const thread = page.locator(`section.boxteam-thread[data-workspace-id="${workspace.workspace_id}"]`
      + `[data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
    await expect(thread.getByTestId('composer-input')).toBeVisible({ timeout: 15_000 });
    await expect(thread.getByTestId('composer-input')).toBeEditable();
    const body = await response.json();
    expect(body.data.session.session_id).toBe(owner.sessionId);
    expect(body.data.session.workspace_id).toBe(workspace.workspace_id);
    expect(body.data.thread_id).toBe(owner.threadId);
    expect(response.status()).toBe(200);
    expect(body.request_id).toBe(response.headers()['x-request-id']);
    const elapsed = performance.now() - started;
    result.sessionOpenMs.push(elapsed);
    result.samples.push({ phase: result.phase, path: new URL(response.url()).pathname,
      requestId: body.request_id, bytes: Buffer.byteLength(JSON.stringify(body)), elapsedMs: elapsed });
    perf.record('end', 'ui.session.open', { stage_id: crypto.randomUUID(), elapsed_ns: Math.round(elapsed * 1e6), status: 'passed', inclusive: true },
      { cache_state: 'alternating_existing_sessions' });
    await observer.checkpoint('session.open', index);

  }

  expect(Math.max(...result.sessionOpenMs), '每次接受会话到正确Composer可操作的总预算').toBeLessThanOrEqual(2000);
  result.domNodes = await page.locator('*').count();
  expect(result.domNodes).toBeLessThan(20_000);
  expect(result.pageErrors).toEqual([]);
  expect(result.forbiddenExecutions).toEqual([]);
  result.phase = 'passed';
} catch (error) {
  failure = error;
  result.observations = observations;
  result.pendingRequests = [...pending].map(([request, started]) => ({ url: request.url(), elapsedMs: performance.now() - started }));
  result.error = error instanceof Error ? error.stack : String(error);
  if (page) {
    try { await page.screenshot({ path: resultPath.replace(/\.json$/, '-failure.png'), fullPage: true }); }
    catch (diagnostic) { failure = new AggregateError([failure, diagnostic], '验收和截图均失败'); }
  }
} finally {
  try { await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n'); }
  catch (diagnostic) { failure = failure ? new AggregateError([failure, diagnostic], '验收和结果保存均失败') : diagnostic; }
  try { await run.close(); }
  catch (cleanup) { failure = failure ? new AggregateError([failure, cleanup], '验收和清理均失败') : cleanup; }
}
if (failure) throw failure;
