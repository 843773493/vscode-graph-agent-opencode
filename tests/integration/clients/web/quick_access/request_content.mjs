import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!base || !output || !screenshot) throw new Error('请求搜索验收缺少隔离目标或产物路径');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_request_content.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], searches: [], locations: [], fullLogs: [],
  boundary: '真实 Gateway／正式 Workbench；原执行入口生成日志；model_stream 复用协议回放，不预写日志或搜索状态' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, reducedMotion: 'reduce' });
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/search' || path === '/api/v1/search' || path === '/api/v1/sessions/history/search') result.searches.push({ path, body: request.postDataJSON() });
    if (path.endsWith('/llm-request-logs/locate')) result.locations.push({ path, body: request.postDataJSON() });
    if (path.endsWith('/llm-request-logs')) result.fullLogs.push(path);
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const api = (path, body) => page.evaluate(async ({ path, body }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': token, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, body });
  await api('/api/gateway/users/guest', { tracking: { source: 'request-content' } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  await api('/api/gateway/health'); await api('/api/gateway/workspaces'); await api('/api/v1/workspace');
  const session = await api('/api/v1/sessions', { title: '请求原文定位验收' });
  const main = (await api(`/api/v1/sessions/${session.session_id}/main-thread`)).thread_id;
  await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: main,
    interactions: [JSON.parse(process.env.BOXTEAM_REQUEST_SEARCH_INTERACTION)] });
  const query = '请求定位😀词';
  const accepted = await api(`/api/v1/sessions/${session.session_id}/threads/${main}/messages`,
    { message: { content: `开头😀\n${query}，再次${query}\n结束` }, run: {} });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 30_000 }).toBe('completed');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('treeitem', { name: /^请求原文定位验收/ })).toBeVisible();
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
  const quick = page.locator('.quick-input-widget');
  await command('workbench.action.quickOpen', 'content ' + query);
  await expect(quick).toContainText('LLM 请求记录');
  expect(result.searches).toHaveLength(0);
  result.unselectedHasNoContentQuery = true;
  const openSearch = async () => {
    await quick.locator('[data-quick-input-id="content-category-request"]').click();
    await expect(quick).toContainText('LLM 请求记录 · 本次查询完成');
  };
  await openSearch();
  const candidates = quick.locator(`[data-quick-input-id^="request:"][data-quick-input-id*="/${session.session_id}/"]`);
  expect(await candidates.count()).toBeGreaterThanOrEqual(2);
  // 首次接受走真实键盘，并先核活动身份，避免动画期间点击重排中的列表。
  await expect(quick.locator('.monaco-list-row.focused [data-quick-input-id]')).toHaveAttribute(
    'data-quick-input-id', await candidates.first().getAttribute('data-quick-input-id'));
  await expect(quick.locator('input')).toBeFocused();
  await page.keyboard.press('Enter');
  const pane = page.locator(`section.boxteam-thread[data-session-id="${session.session_id}"][data-thread-id="${main}"]`);
  const mark = pane.getByLabel('请求日志命中原文').locator('mark');
  await expect(mark).toHaveText(query);
  await expect(pane.locator('.request-log-card')).toContainText('响应预览');
  const visibleMark = () => mark.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const panel = element.closest('.panel-list').getBoundingClientRect();
    return rect.top >= panel.top && rect.bottom <= panel.bottom;
  });
  await expect.poll(visibleMark).toBe(true);
  expect(result.locations).toHaveLength(1);
  result.exactRecordLocated = true;
  const first = result.locations[0].body;
  await command('workbench.action.quickOpen', 'content ' + query); await openSearch();
  await candidates.nth(1).click();
  await expect.poll(() => result.locations.length).toBe(2);
  await expect(mark).toHaveText(query); await expect.poll(visibleMark).toBe(true);
  expect(result.locations[1].body.file_name).toBe(first.file_name);
  expect(result.locations[1].body.start_codepoint).toBeGreaterThan(first.start_codepoint);
  expect(await pane.count()).toBe(1);
  result.samePaneNewLocation = true;
  await pane.getByRole('button', { name: '关闭请求定位', exact: true }).click();
  await expect(mark).toHaveCount(0);
  // 改动请求参数来证明真实 owner 拒绝旧版本；不替换定位响应。
  const pattern = '**/llm-request-logs/locate';
  await page.route(pattern, route => route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), content_version: '0'.repeat(64) }) }));
  await command('workbench.action.quickOpen', 'content ' + query); await openSearch();
  const rejected = page.waitForResponse(response => response.url().endsWith('/llm-request-logs/locate') && response.status() === 409);
  await candidates.first().click();
  expect((await (await rejected).json()).detail).toMatchObject({
    owner: 'llm_request_log', code: 'stale_request_log_reference', reason: 'record_changed',
  });
  await expect(pane.getByRole('alert')).toContainText('命中的请求日志已变化');
  await expect(mark).toHaveCount(0);
  result.staleReferenceRejected = true;
  expect(result.searches.every(request => request.path === '/api/gateway/search' && request.body.queries.length === 1 && request.body.queries[0].category === 'llm_request')).toBe(true);
  expect(result.fullLogs).toHaveLength(0);
  result.noFullLogRead = true;
  // 使用真实执行产出的日志，核验原API拒绝及联邦诊断；不预写日志或复制SSE解码器。
  const workspace = await api('/api/v1/workspace');
  const health = await api('/api/gateway/health');
  const owner = { gateway_id: health.gateway_id, workspace_id: workspace.workspace_id };
  const searchQuery = { category: 'llm_request', operation: 'content', match_mode: 'literal', query, limit: 1 };
  const search = { workspace_id: workspace.workspace_id, ...searchQuery };
  const originalPage = await api('/api/v1/search', search);
  const cursor = originalPage.llm_requests.cursor;
  expect(cursor).toBeTruthy();
  const rejectCursor = async (cursor, status, code, reason) => {
    const rejected = await page.evaluate(async ({ search, cursor }) => {
      const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch('/api/v1/search', { method: 'POST',
        headers: { 'X-Local-Token': token, 'Content-Type': 'application/json' }, body: JSON.stringify({ ...search, cursor }) });
      return { status: response.status, requestId: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { search, cursor });
    expect(rejected.status).toBe(status);
    expect(rejected.body.request_id).toBe(rejected.requestId);
    expect(rejected.body.detail).toMatchObject({ owner: 'llm_request_log', code, reason });
    expect(rejected.body.detail.message).toBeTruthy();
    return rejected;
  };
  const malformed = await rejectCursor('%%%', 400, 'invalid_request_log_cursor', 'malformed');
  const changedQuery = JSON.parse(Buffer.from(cursor, 'base64url').toString());
  changedQuery.query = '另一个查询';
  const mismatched = await rejectCursor(Buffer.from(JSON.stringify(changedQuery)).toString('base64url'), 400,
    'invalid_request_log_cursor', 'query_mismatch');
  await api('/api/v1/sessions', { title: '日志查询后的目录变化' });
  const stale = await rejectCursor(cursor, 409, 'stale_request_log_cursor', 'catalog_changed');
  result.cursorDiagnostics = { owner, searchQuery, cursor, malformed, mismatched, stale };
  result.phase = 'passed';
} catch (error) {
  failure = error; result.error = String(error?.stack ?? error);
  if (page) await page.screenshot({ path: screenshot }).catch(error => { result.screenshotError = String(error); });
} finally {
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
