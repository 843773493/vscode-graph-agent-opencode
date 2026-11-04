import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH, query = process.env.BOXTEAM_TRACE_SEARCH_QUERY;
if (!base || !output || !screenshot || !query) throw new Error('Trace 验收缺少隔离目标或产物路径');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_trace_content.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], searches: [], locations: [], fullTraces: [],
  boundary: '真实 Gateway／正式 Workbench；消息执行及 fork 创建关键状态；model_stream 复用回放，不预写事件或归属索引' };
let page;
const failures = [];
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, reducedMotion: 'reduce' });
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/search' || path === '/api/v1/search' || path === '/api/v1/sessions/history/search') result.searches.push({ path, body: request.postDataJSON() });
    if (path.endsWith('/traces/locate')) result.locations.push({ path, body: request.postDataJSON() });
    if (path.endsWith('/traces')) result.fullTraces.push(path);
  });
  const api = (path, body) => page.evaluate(async ({ path, body }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': token, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, body });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await api('/api/gateway/users/guest', { tracking: { source: 'trace-content' } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health'), workspace = await api('/api/v1/workspace');
  await api('/api/gateway/workspaces');
  const session = await api('/api/v1/sessions', { title: 'Trace 原文定位验收' });
  const main = (await api(`/api/v1/sessions/${session.session_id}/main-thread`)).thread_id;
  const interactions = JSON.parse(process.env.BOXTEAM_TRACE_SEARCH_INTERACTIONS);
  const send = async (thread, content) => {
    await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: thread, interactions: [interactions.shift()] });
    const accepted = await api(`/api/v1/sessions/${session.session_id}/threads/${thread}/messages`, { message: { content }, run: {} });
    await expect.poll(async () => {
      const job = await api(`/api/v1/jobs/${accepted.job_id}`);
      if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
      return job.status;
    }, { timeout: 30_000 }).toBe('completed');
    return accepted;
  };
  await send(main, '准备一个可 fork 的执行点');
  const source = await api(`/api/v1/sessions/${session.session_id}/fork-source-point`);
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, {
    idempotency_key: 'trace-content-child', source_uri: source.source_uri, source_view_id: source.source_view_id,
    source_view_revision: source.source_view_revision, anchor_item_id: source.anchor_item_id,
    target_container_uri: source.child_container_uri, mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  const child = fork.thread_id;
  expect(child).not.toBe(main);
  await send(child, '生成仅属于 child 本轮执行的响应');
  result.phase = 'wait-navigation';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('treeitem', { name: /^Trace 原文定位验收/ })).toBeVisible();
  expect(await page.locator(`section.boxteam-thread[data-thread-id="${child}"]`).count()).toBe(0);
  result.phase = 'choose-category';
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const quick = page.locator('.quick-input-widget');
  await command('workbench.action.quickOpen', 'content ' + query);
  await expect(quick.locator('[data-quick-input-id="content-category-trace"]')).toBeVisible();
  expect(result.searches).toHaveLength(0);
  result.unselectedHasNoContentQuery = true;
  const openSearch = async () => {
    await quick.locator('[data-quick-input-id="content-category-trace"]').click();
    await expect(quick).toContainText('Trace · 本次查询完成');
  };
  await openSearch();
  const rows = quick.locator('.monaco-list-row').filter({ has: page.locator(`[data-quick-input-id*="/${child}#trace/"]`) }).filter({ hasText: 'Trace agent_end' });
  await expect(rows).toHaveCount(2);
  result.phase = 'open-unloaded-child';
  await rows.first().locator('[data-quick-input-id]').click();
  const pane = page.locator(`section.boxteam-thread[data-session-id="${session.session_id}"][data-thread-id="${child}"]`);
  const mark = pane.getByLabel('Trace 命中原文').locator('mark');
  await expect(mark).toHaveText(query);
  await expect(pane.locator('.event-queue-card')).toContainText('agent_end');
  const visibleMark = () => mark.evaluate(element => {
    const rect = element.getBoundingClientRect(), panel = element.closest('.panel-list').getBoundingClientRect();
    return rect.top >= panel.top && rect.bottom <= panel.bottom;
  });
  await expect.poll(visibleMark).toBe(true);
  expect(result.locations).toHaveLength(1);
  const first = result.locations[0].body;
  expect(first.thread_id).toBe(child);
  result.unopenedChildLocated = true;
  await command('workbench.action.quickOpen', 'content ' + query); await openSearch();
  await rows.nth(1).locator('[data-quick-input-id]').click();
  await expect.poll(() => result.locations.length).toBe(2);
  await expect(mark).toHaveText(query); await expect.poll(visibleMark).toBe(true);
  expect(result.locations[1].body.event_id).toBe(first.event_id);
  expect(result.locations[1].body.start_codepoint).toBeGreaterThan(first.start_codepoint);
  expect(await pane.count()).toBe(1);
  result.samePaneNewLocation = true;
  await pane.getByRole('button', { name: '关闭 Trace 定位', exact: true }).click();
  await expect(mark).toHaveCount(0);
  expect(result.searches.every(request => request.path === '/api/gateway/search' && request.body.queries.length === 1 && request.body.queries[0].category === 'trace'
    && (request.body.scope.mode === 'resume' ? request.body.scope.scope.owners : request.body.scope.owners).length === 1
    && (request.body.scope.mode === 'resume' ? request.body.scope.scope.owners : request.body.scope.owners)[0].workspace_id === workspace.workspace_id)).toBe(true);
  result.onlySelectedOwnerAndCategory = true;
  expect(result.fullTraces).toHaveLength(0);
  result.noFullTraceRead = true;
  const owner = { gateway_id: health.gateway_id, workspace_id: workspace.workspace_id };
  const searchQuery = { category: 'trace', operation: 'content', match_mode: 'literal', query, limit: 1 };
  const original = await api('/api/v1/search', { workspace_id: workspace.workspace_id, ...searchQuery });
  expect(original.traces.cursor).toBeTruthy();
  await send(child, '后续执行用于核验原行独立定位');
  const located = await api(`/api/v1/sessions/${session.session_id}/traces/locate`, first);
  expect(located.record.event_id).toBe(first.event_id);
  expect(Array.from(located.text).slice(first.start_codepoint, first.end_codepoint).join('')).toBe(query);
  result.exactRecordAfterAppend = true;
  result.cursorDiagnostics = { owner, query: searchQuery, cursor: original.traces.cursor };
  result.phase = 'reject-stale-reference';
  // 仅改定位请求的版本，真实 owner 负责拒绝；不替换响应、Trace 或搜索状态。
  await page.route('**/traces/locate', route => route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), content_version: '0'.repeat(64) }) }));
  await command('workbench.action.quickOpen', 'content ' + query); await openSearch();
  const rejected = page.waitForResponse(response => response.url().endsWith('/traces/locate') && response.status() === 409);
  await rows.first().locator('[data-quick-input-id]').click();
  expect((await (await rejected).json()).detail).toMatchObject({ owner: 'trace_event_store', code: 'stale_trace_reference', reason: 'record_changed' });
  await expect(pane.getByRole('alert')).toContainText('命中的 Trace 记录已变化或裁剪');
  await expect(mark).toHaveCount(0);
  result.staleReferenceRejected = true;
  result.owner = { ...owner, session_id: session.session_id, main, child };
  result.phase = 'complete';
} catch (error) {
  failures.push(error); result.error = String(error?.stack ?? error);
  if (page) {
    const diagnostics = await Promise.allSettled([
      page.content().then(content => writeFile(screenshot + '.html', content)),
      page.screenshot({ path: screenshot, fullPage: true }),
    ]);
    result.diagnosticErrors = diagnostics.filter(item => item.status === 'rejected').map(item => String(item.reason));
    failures.push(...diagnostics.filter(item => item.status === 'rejected').map(item => item.reason));
  }
} finally {
  try { await run.close(); }
  catch (error) { result.cleanupError = String(error); failures.push(error); }
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { failures.push(error); }
}
if (failures.length === 1) throw failures[0];
if (failures.length > 1) throw new AggregateError(failures, 'Trace 场景、诊断或资源清理失败');
