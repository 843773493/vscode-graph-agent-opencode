import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../../support/e2e_control/browser_client.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`缺少 ${name}`); return value; };
const base = required('BOXTEAM_BROWSER_BASE_URL'), resultPath = required('BOXTEAM_BROWSER_RESULT_PATH');
const screenshotPath = required('BOXTEAM_BROWSER_SCREENSHOT_PATH'), fixture = JSON.parse(required('BOXTEAM_BROWSER_FIXTURE'));
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_tool_results.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], toolLists: [], histories: [],
  detailLoads: [], executionRequests: [], contentReads: [],
  boundary: '真实 Gateway／Backend／Workbench；model_stream 通过消息入口真实执行工具；候选来自精确 child Thread metadata，接受后沿原 around 历史定位' };
const pending = [], collectionErrors = [], requestRecords = new WeakMap();
let page, failure;

try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('工具结果 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1700, height: 1100 } });
  run.addCleanup('工具结果 BrowserContext', () => context.close());
  page = await context.newPage(); page.setDefaultTimeout(20_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const url = new URL(request.url()), path = url.pathname, headers = request.headers();
    const identity = { path, method: request.method(), phase: result.phase,
      gatewayId: headers['x-boxteam-target-gateway-id'], workspaceId: headers['x-boxteam-workspace-id'] };
    if (/^\/api\/v1\/sessions\/[^/]+\/threads\/[^/]+\/tool-results$/.test(path)) {
      Object.assign(identity, { after: url.searchParams.get('after'), viewId: url.searchParams.get('view_id'),
        viewRevision: url.searchParams.get('view_revision'), limit: url.searchParams.get('limit') });
      result.toolLists.push(identity); requestRecords.set(request, identity);
    }
    if (path.endsWith('/history') && request.method() === 'POST') {
      const record = { ...identity, body: request.postDataJSON() };
      result.histories.push(record); requestRecords.set(request, record);
    }
    if (path.endsWith('/history/details') || path.endsWith('/history/detail')) result.detailLoads.push(identity);
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executionRequests.push(identity);
    if (path === '/api/v1/sessions/attachments/content') result.contentReads.push({ ...identity, query: url.search });
  });
  page.on('requestfinished', request => {
    const record = requestRecords.get(request);
    if (!record) return;
    pending.push((async () => {
      const response = await request.response(), body = await response.json();
      Object.assign(record, { status: response.status(), requestId: body.request_id, header: response.headers()['x-request-id'], data: body.data });
      expect(record.requestId).toBe(record.header);
    })().catch(cause => collectionErrors.push(String(cause))));
  });

  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
      } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, owner, body });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces?check_health=false');
  expect(health.gateway_id).toBe(fixture.gatewayId);
  expect(targets.items).toContainEqual(expect.objectContaining({ gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId }));
  const owner = { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId };
  expect((await api('/api/v1/workspace', owner)).workspace_id).toBe(owner.workspaceId);
  const session = await api('/api/v1/sessions', owner, { title: '工具结果 Quick Access child' });
  const mainThread = (await api(`/api/v1/sessions/${session.session_id}/main-thread`, owner)).thread_id;
  const source = await api(`/api/v1/sessions/${session.session_id}/fork-source-point`, owner);
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, owner, {
    idempotency_key: 'tool-result-quick-access-child', source_uri: source.source_uri,
    source_view_id: source.source_view_id, source_view_revision: source.source_view_revision,
    anchor_item_id: source.anchor_item_id, target_container_uri: source.child_container_uri,
    mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  const threadId = fork.thread_id;
  expect(threadId).not.toBe(mainThread);
  result.owner = { ...owner, sessionId: session.session_id, threadId };
  result.mainThreadId = mainThread;

  result.phase = 'execute-child-tool';
  await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: threadId, interactions: fixture.interactions });
  const accepted = await api(`/api/v1/sessions/${session.session_id}/threads/${threadId}/messages`, owner,
    { message: { content: '请真实调用工具并返回结果。' }, run: {} });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`, owner);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 45_000 }).toBe('completed');
  result.turnId = accepted.turn_id;

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const threadSelector = `section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${session.session_id}"][data-thread-id="${threadId}"]`;
  await command('_workbench.open', `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/${session.session_id}/threads/${threadId}`);
  const thread = page.locator(threadSelector);
  await expect(thread.locator('textarea[data-testid="composer-input"]')).toBeEnabled({ timeout: 30_000 });
  await expect(thread.locator('.chat-tool-row')).toHaveCount(1);

  result.phase = 'browse-tool-results';
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await command('boxteam.quickAccess.resources', owner);
  await expect(input).toHaveValue('resource ');
  const expectedIdFor = itemId => JSON.stringify([owner.gatewayId, owner.workspaceId, session.session_id, threadId, 'tool-result', itemId]);
  const row = quick.locator('.monaco-list-row').filter({ hasText: fixture.toolName });
  await expect(row).toBeVisible();
  await expect.poll(() => result.toolLists.filter(item => item.phase === result.phase && item.data).length).toBe(1);
  const listed = result.toolLists.find(item => item.phase === result.phase).data;
  const toolMetadata = listed.items.find(item => item.tool_name === fixture.toolName);
  expect(toolMetadata).toBeTruthy();
  result.itemId = toolMetadata.item_id;
  await expect(row.locator('[data-quick-input-id]')).toHaveAttribute('data-quick-input-id', expectedIdFor(result.itemId));
  expect(result.detailLoads).toEqual([]); expect(result.contentReads).toEqual([]);
  result.browseOnly = true;

  result.phase = 'accept-tool-result';
  await row.click();
  await expect(quick).toBeHidden();
  await expect.poll(() => result.histories.some(item => item.phase === result.phase)).toBe(true);
  const locateRequest = result.histories.find(item => item.phase === result.phase);
  expect(locateRequest).toMatchObject({ method: 'POST', gatewayId: owner.gatewayId, workspaceId: owner.workspaceId,
    body: { direction: 'around', anchor_turn_id: accepted.turn_id, anchor_tool_result: result.itemId } });
  await expect(thread.locator(`[data-tool-result-item-id="${result.itemId}"]`)).toBeVisible({ timeout: 30_000 });
  const toolRow = thread.locator(`[data-tool-result-item-id="${result.itemId}"]`);
  await expect(toolRow.locator('.chat-tool-summary')).toHaveAttribute('aria-expanded', 'true');
  await expect.poll(() => result.detailLoads.filter(item => item.phase === result.phase).length).toBe(0);
  expect(result.executionRequests).toHaveLength(1);
  result.locatedOriginalToolRow = true;
  result.noReplayOnAccept = true;
  expect(result.contentReads).toEqual([]);
  expect(result.pageErrors).toEqual([]);
  await Promise.all(pending);
  expect(collectionErrors).toEqual([]);
  expect(result.toolLists.every(item => item.status === 200 && item.requestId === item.header)).toBe(true);
  expect(result.toolLists.every(item => item.path === `/api/v1/sessions/${session.session_id}/threads/${threadId}/tool-results`)).toBe(true);
  expect(result.toolLists.every(item => item.gatewayId === owner.gatewayId && item.workspaceId === owner.workspaceId)).toBe(true);
  expect(result.toolLists.every(item => item.data.items.every(tool => !Object.keys(tool).some(key => ['content', 'body', 'result', 'input'].includes(key))))).toBe(true);
  expect(result.histories.filter(item => item.phase === 'accept-tool-result')).toHaveLength(1);
  result.phase = 'complete';
} catch (cause) {
  failure = cause; result.error = String(cause.stack ?? cause);
} finally {
  if (page) {
    try { await page.screenshot({ path: screenshotPath, fullPage: false }); }
    catch (cause) { failure = failure ? new AggregateError([failure, cause], '工具结果验收与截图均失败') : cause; }
  }
  try { await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n'); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '工具结果验收与产物保存均失败') : cause; }
  try { await run.close(); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '工具结果验收与清理均失败') : cause; }
}
if (failure) throw failure;
