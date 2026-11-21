import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../../support/e2e_control/browser_client.mjs';
import { newOperationId } from '../../../../../support/operation_ids.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`缺少 ${name}`); return value; };
const base = required('BOXTEAM_BROWSER_BASE_URL'), resultPath = required('BOXTEAM_BROWSER_RESULT_PATH');
const screenshotPath = required('BOXTEAM_BROWSER_SCREENSHOT_PATH'), fixture = JSON.parse(required('BOXTEAM_BROWSER_FIXTURE'));
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_tool_results.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], toolLists: [], histories: [],
  forkSourcePoints: [], forkWrites: [], detailLoads: [], executionRequests: [], contentReads: [],
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
    if (/\/fork-source-point$/.test(path) && request.method() === 'GET') {
      result.forkSourcePoints.push(identity); requestRecords.set(request, identity);
    }
    if (/\/fork-context$/.test(path) && request.method() === 'POST') {
      Object.assign(identity, { body: request.postDataJSON() });
      result.forkWrites.push(identity); requestRecords.set(request, identity);
    }
    if (path.endsWith('/history') && request.method() === 'POST') {
      const record = { ...identity, body: request.postDataJSON() };
      result.histories.push(record); requestRecords.set(request, record);
    }
    if (path.endsWith('/history/details') || path.endsWith('/history/detail')) result.detailLoads.push(identity);
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executionRequests.push(identity);
    if (path === '/api/v1/sessions/attachments/content'
      || (path === '/api/v1/resources/navigation' && request.method() === 'POST'
        && request.postDataJSON()?.operation === 'read')) {
      result.contentReads.push({ ...identity, query: url.search });
    }
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
  result.owner = { ...owner, sessionId: session.session_id };
  result.mainThreadId = mainThread;

  result.phase = 'execute-source-seed';
  await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: mainThread,
    interactions: [fixture.seedInteraction] });
  const seed = await api(`/api/v1/sessions/${session.session_id}/threads/${mainThread}/messages`, owner,
    { operation_id: newOperationId(), message: { content: '准备上下文分支。' }, run: {} });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${seed.job_id}`, owner);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 45_000 }).toBe('completed');

  result.phase = 'fork-source-point';
  const source = await api(`/api/v1/sessions/${session.session_id}/fork-source-point`, owner);
  expect(source.source_uri).toContain(`/resources/session/${session.session_id}/threads/${mainThread}`);
  expect(source.source_view_id).toBeTruthy();
  expect(Number.isInteger(source.source_view_revision)).toBe(true);
  result.forkSource = source;
  result.phase = 'fork-child-thread';
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, owner, {
    idempotency_key: 'tool-result-quick-access-child', source_uri: source.source_uri,
    source_view_id: source.source_view_id, source_view_revision: source.source_view_revision,
    anchor_item_id: source.anchor_item_id, target_container_uri: source.child_container_uri,
    mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  const threadId = fork.thread_id;
  expect(threadId).not.toBe(mainThread);
  const ownedSourceUri = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/${session.session_id}/threads/${mainThread}`;
  expect(fork.source_uri).toBe(ownedSourceUri);
  expect(fork.source_view_id).toBe(source.source_view_id);
  expect(fork.source_view_revision).toBe(source.source_view_revision);
  result.owner.threadId = threadId;
  result.childSource = { sourceUri: fork.source_uri, sourceViewId: fork.source_view_id,
    sourceViewRevision: fork.source_view_revision, threadId };
  await expect.poll(() => result.forkSourcePoints.some(item => item.data)
    && result.forkWrites.some(item => item.data)).toBe(true);
  expect(result.forkSourcePoints).toHaveLength(1);
  expect(result.forkWrites).toHaveLength(1);
  const sourceRead = result.forkSourcePoints[0];
  const forkWrite = result.forkWrites[0];
  expect(sourceRead).toMatchObject({ status: 200, requestId: sourceRead.header,
    data: { source_uri: source.source_uri, source_view_id: source.source_view_id,
      source_view_revision: source.source_view_revision } });
  expect(forkWrite).toMatchObject({ status: 200, requestId: forkWrite.header,
    body: { source_uri: source.source_uri, source_view_id: source.source_view_id,
      source_view_revision: source.source_view_revision, anchor_item_id: source.anchor_item_id,
      creation_kind: 'new_child_thread' } });
  expect(forkWrite.data).toMatchObject({ thread_id: threadId, source_uri: ownedSourceUri,
    source_view_id: source.source_view_id, source_view_revision: source.source_view_revision });
  result.forkCommitted = true;

  result.phase = 'execute-child-tool';
  await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: threadId, interactions: fixture.interactions });
  const accepted = await api(`/api/v1/sessions/${session.session_id}/threads/${threadId}/messages`, owner,
    { operation_id: newOperationId(), message: { content: '请真实调用工具并返回结果。' }, run: {} });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`, owner);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 45_000 }).toBe('completed');
  result.turnId = accepted.turn_id;

  result.phase = 'open-child-thread';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const threadSelector = `section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${session.session_id}"][data-thread-id="${threadId}"]`;
  const threadUri = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/${session.session_id}/threads/${threadId}`;
  await command('workbench.action.quickOpen', threadUri);
  await page.locator('.quick-input-widget').getByText('打开链接', { exact: true }).click();
  const thread = page.locator(threadSelector);
  await expect(thread.locator('textarea[data-testid="composer-input"]')).toBeEnabled({ timeout: 30_000 });
  await expect.poll(() => result.histories.some(item => item.phase === result.phase && item.status === 200)).toBe(true);
  await Promise.all(pending);

  result.phase = 'browse-tool-results';
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const invocationToolName = 'invoke_extension_tool';
  await command('boxteam.quickAccess.resources', owner);
  await expect(input).toHaveValue('resource ');
  const expectedIdFor = itemId => JSON.stringify([owner.gatewayId, owner.workspaceId, session.session_id, threadId, 'tool-result', itemId]);
  const row = quick.locator('.monaco-list-row').filter({ hasText: invocationToolName });
  await expect(row).toBeVisible();
  await expect.poll(() => result.toolLists.filter(item => item.phase === result.phase && item.data).length).toBe(1);
  const listed = result.toolLists.find(item => item.phase === result.phase).data;
  const toolMetadata = listed.items.find(item => item.tool_name === invocationToolName);
  expect(toolMetadata).toBeTruthy();
  expect(listed).toMatchObject({ session_id: session.session_id, thread_id: threadId,
    source_view_id: expect.any(String), source_view_revision: expect.any(Number) });
  expect(result.toolLists.find(item => item.phase === result.phase)).toMatchObject({
    after: null, viewId: null, viewRevision: null, limit: '100', status: 200,
  });
  expect(Object.keys(toolMetadata).sort()).toEqual(['item_id', 'status', 'tool_call_id', 'tool_name', 'turn_id']);
  expect(toolMetadata.turn_id).toBe(accepted.turn_id);
  expect(toolMetadata.tool_call_id).toContain(fixture.toolCallId);
  result.itemId = toolMetadata.item_id;
  await expect(row.locator('[data-quick-input-id]')).toHaveAttribute('data-quick-input-id', expectedIdFor(result.itemId));
  expect(result.detailLoads).toEqual([]); expect(result.contentReads).toEqual([]);
  expect(result.histories.filter(item => ['browse-tool-results', 'cancel-tool-results'].includes(item.phase))).toEqual([]);
  result.browseOnly = true;

  result.phase = 'cancel-tool-results';
  await page.keyboard.press('Escape');
  await expect(quick).toBeHidden();
  await Promise.all(pending);
  expect(result.toolLists.filter(item => item.phase === 'cancel-tool-results')).toEqual([]);
  expect(result.histories.filter(item => ['browse-tool-results', 'cancel-tool-results'].includes(item.phase))).toEqual([]);
  expect(result.detailLoads.filter(item => ['browse-tool-results', 'cancel-tool-results'].includes(item.phase))).toEqual([]);
  expect(result.contentReads.filter(item => ['browse-tool-results', 'cancel-tool-results'].includes(item.phase))).toEqual([]);
  result.cancelledWithoutBodyRead = true;

  result.phase = 'accept-tool-result';
  await command('boxteam.quickAccess.resources', owner);
  await expect(input).toHaveValue('resource ');
  const acceptedRow = quick.locator('.monaco-list-row').filter({ hasText: invocationToolName });
  await expect(acceptedRow).toBeVisible();
  await expect.poll(() => result.toolLists.filter(item => item.phase === result.phase && item.data).length).toBe(1);
  const acceptedList = result.toolLists.find(item => item.phase === result.phase).data;
  expect(acceptedList.source_view_id).toBe(listed.source_view_id);
  expect(acceptedList.source_view_revision).toBe(listed.source_view_revision);
  const acceptedMetadata = acceptedList.items.find(item => item.item_id === result.itemId);
  expect(acceptedMetadata).toEqual(toolMetadata);
  await acceptedRow.click();
  await expect(quick).toBeHidden();
  await expect.poll(() => result.histories.some(item => item.phase === result.phase)).toBe(true);
  const locateRequest = result.histories.find(item => item.phase === result.phase);
  expect(locateRequest).toMatchObject({ method: 'POST', gatewayId: owner.gatewayId, workspaceId: owner.workspaceId,
    body: { direction: 'around', anchor_turn_id: accepted.turn_id, anchor_tool_result: result.itemId } });
  await expect.poll(() => Boolean(locateRequest.status)).toBe(true);
  expect(locateRequest.status).toBe(200);
  expect(locateRequest.requestId).toBe(locateRequest.header);
  expect(locateRequest.data.located_tool_result).toEqual(toolMetadata);
  await expect(thread.locator(`[data-tool-result-item-id="${result.itemId}"]`)).toBeVisible({ timeout: 30_000 });
  const toolRow = thread.locator(`[data-tool-result-item-id="${result.itemId}"]`);
  await expect(toolRow.locator('.chat-tool-summary')).toHaveAttribute('aria-expanded', 'true');
  await expect.poll(() => result.detailLoads.filter(item => item.phase === result.phase).length).toBe(0);
  expect(result.executionRequests).toHaveLength(2);
  expect(result.executionRequests.filter(item => item.phase === 'execute-source-seed')).toHaveLength(1);
  expect(result.executionRequests.filter(item => item.phase === 'execute-child-tool')).toHaveLength(1);
  result.locatedOriginalToolRow = true;
  result.noReplayOnAccept = true;
  expect(result.contentReads).toEqual([]);
  expect(result.pageErrors).toEqual([]);
  await Promise.all(pending);
  expect(collectionErrors).toEqual([]);
  expect(result.toolLists.every(item => item.status === 200 && item.requestId === item.header)).toBe(true);
  expect(result.toolLists.every(item => item.path === `/api/v1/sessions/${session.session_id}/threads/${threadId}/tool-results`)).toBe(true);
  expect(result.toolLists.every(item => item.data.session_id === session.session_id && item.data.thread_id === threadId)).toBe(true);
  expect(result.toolLists.every(item => item.gatewayId === owner.gatewayId && item.workspaceId === owner.workspaceId)).toBe(true);
  expect(result.toolLists.every(item => item.data.items.every(tool => !Object.keys(tool).some(key => ['content', 'body', 'result', 'input'].includes(key))))).toBe(true);
  expect(result.toolLists.every(item => item.data.source_view_id && Number.isInteger(item.data.source_view_revision))).toBe(true);
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
