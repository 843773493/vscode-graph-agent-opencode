// Integration：仅 B 的外部 HTTP 边界无响应，A／Gateway／Workbench 均为真实产品。
import { dirname, join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`会话恢复验收缺少 ${name}`);
  return value;
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const otherUrl = required('BOXTEAM_HEALTH_OTHER_URL');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_thread_restore_health.py');
const result = { phase: 'initialize', classification: 'Integration', pageErrors: [], requests: [],
  boundary: '仅B健康HTTP服务为受控替身；A真实受管后端、真实Gateway及正式Workbench，未预写会话／浏览器状态' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('health-browser', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('health-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/api/gateway/workspaces') result.requests.push({ phase: result.phase, url: request.url() });
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const api = (path, init = {}) => page.evaluate(async ({ path, init }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, body }));
    }
    return { data: body.data, requestId: body.request_id };
  }, { path, init });
  const listing = await api('/api/gateway/workspaces?check_health=false');
  const owner = listing.data.active_workspace;
  const headers = { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id };
  const created = await api('/api/v1/sessions', { method: 'POST', headers, body: JSON.stringify({ title: '独立恢复A' }) });
  const session = created.data;
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, { headers });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  await command('workbench.action.closeAllEditors');
  expect((await fetch(`${otherUrl}/block`)).ok).toBe(true);
  result.phase = 'restore-A-with-B-unresponsive';
  const started = performance.now();
  const [bootstrap] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/sessions/${session.session_id}/bootstrap`
      && new URL(response.url()).searchParams.get('thread_id') === main.data.thread_id, { timeout: 15_000 }),
    command('_workbench.open', `boxteam-thread://${owner.gateway_id}/${owner.workspace_id}/${session.session_id}/${main.data.thread_id}`,
      [undefined, { pinned: true }]),
  ]);
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspace_id}"]`
    + `[data-session-id="${session.session_id}"][data-thread-id="${main.data.thread_id}"]`);
  await expect(thread.getByTestId('composer-input')).toBeVisible({ timeout: 15_000 });
  await expect(thread.getByTestId('composer-input')).toBeEditable();
  result.openMs = performance.now() - started;
  const body = await bootstrap.json();
  expect(bootstrap.status()).toBe(200);
  expect(body.request_id).toBe(bootstrap.headers()['x-request-id']);
  expect(body.data.session.session_id).toBe(session.session_id);
  expect(body.data.session.workspace_id).toBe(owner.workspace_id);
  expect(body.data.thread_id).toBe(main.data.thread_id);
  result.bootstrap = { owner, sessionId: session.session_id, threadId: main.data.thread_id, requestId: body.request_id };
  result.restoreRequests = result.requests.filter(item => item.phase === result.phase);
  // 先保留失败样本，再检查请求边界；不以删掉健康检查错误或改大预算提速。
  expect(result.openMs, '无关B不可达不应占用A会话打开的总预算').toBeLessThanOrEqual(2000);
  expect(result.restoreRequests.length).toBeGreaterThan(0);
  for (const request of result.restoreRequests) expect(new URL(request.url).searchParams.get('check_health')).toBe('false');
  result.phase = 'explicit-health-still-reports-B';
  const healthStart = performance.now();
  const health = await api('/api/gateway/workspaces?check_health=true');
  result.healthMs = performance.now() - healthStart;
  const target = health.data.items.find(item => item.workspace_id === required('BOXTEAM_HEALTH_OTHER_ID'));
  expect(target.status).toBe('offline');
  expect(target.services.workspace_api.error).toBeTruthy();
  result.health = { status: target.status, services: target.services, requestId: health.requestId };
  await expect(thread.getByTestId('composer-input')).toBeEditable();
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = error instanceof Error ? error.stack : String(error);
  if (page) {
    try { await page.screenshot({ path: join(dirname(output), 'thread-restore-health-failure.png'), fullPage: true }); }
    catch (diagnostic) { failure = new AggregateError([failure, diagnostic], '恢复验收与截图均失败'); }
  }
} finally {
  try { await fetch(`${otherUrl}/release`); }
  catch (cleanup) { failure = failure ? new AggregateError([failure, cleanup], '恢复验收与B释放失败') : cleanup; }
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (diagnostic) { failure = failure ? new AggregateError([failure, diagnostic], '恢复结果保存失败') : diagnostic; }
  try { await run.close(); }
  catch (cleanup) { failure = failure ? new AggregateError([failure, cleanup], '恢复验收与清理失败') : cleanup; }
}
if (failure) throw failure;
