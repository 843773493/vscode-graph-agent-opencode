import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../../support/e2e_control/browser_client.mjs';
import { newOperationId } from '../../../../../support/operation_ids.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`Thread 引用页面缺少 ${name}`); return value; };
const base = required('BOXTEAM_BROWSER_BASE_URL'), resultPath = required('BOXTEAM_BROWSER_RESULT_PATH');
const screenshotPath = required('BOXTEAM_BROWSER_SCREENSHOT_PATH');
const interactions = JSON.parse(required('BOXTEAM_THREAD_REFERENCE_INTERACTIONS'));
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_thread_references.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], apiRequests: [], threadAddresses: [], fileAddresses: [],
  resourceReads: [], executions: [], receipts: [],
  boundary: '真实 Gateway／Backend／正式 Workbench；Session、main与child由产品 API／fork创建；模型仅真实提交一条源消息；引用复制与文件加入草稿不打开文件、不读取正文、不执行消息' };
const pendingReceipts = [], responseErrors = [], records = new WeakMap();
let page, failure;

try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Thread 引用 Chromium', () => browser.close());
  const browserContext = await browser.newContext({ viewport: { width: 1750, height: 1050 }, permissions: ['clipboard-read', 'clipboard-write'] });
  run.addCleanup('Thread 引用 BrowserContext', () => browserContext.close());
  page = await browserContext.newPage(); page.setDefaultTimeout(20_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const url = new URL(request.url()), path = url.pathname;
    if (!path.startsWith('/api/')) return;
    const record = { phase: result.phase, path, method: request.method(), body: request.method() === 'GET' ? undefined : request.postDataJSON() };
    result.apiRequests.push(record);
    if (path === '/api/gateway/resource-addresses/thread') result.threadAddresses.push(record);
    if (path === '/api/gateway/resource-addresses/parse') result.fileAddresses.push(record);
    if (path === '/api/v1/resources/navigation' || /\/workspace\/files\/(raw|content)$/.test(path)
      || path === '/api/v1/sessions/attachments/content') result.resourceReads.push(record);
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executions.push(record);
    if (path === '/api/gateway/resource-addresses/thread' || path === '/api/gateway/resource-addresses/parse') {
      records.set(request, record);
    }
  });
  page.on('requestfinished', request => {
    const record = records.get(request);
    if (!record) return;
    pendingReceipts.push((async () => {
      const response = await request.response(), body = await response.json();
      record.receipt = { status: response.status(), requestId: body.request_id, header: response.headers()['x-request-id'], data: body.data };
      result.receipts.push(record);
    })().catch(cause => responseErrors.push(String(cause))));
  });

  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
      } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json(), header = response.headers.get('X-Request-ID');
    if (!response.ok || !payload.request_id || payload.request_id !== header) {
      throw new Error(JSON.stringify({ path, status: response.status, payload, header }));
    }
    return payload.data;
  }, { path, owner, body });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces?check_health=false');
  const owner = { gatewayId: health.gateway_id, workspaceId: workspaces.active_workspace.workspace_id };
  expect(workspaces.active_workspace).toEqual({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId });
  expect(workspaces.items).toContainEqual(expect.objectContaining({ gateway_id: owner.gatewayId,
    workspace_id: owner.workspaceId, name: expect.any(String), status: expect.any(String) }));
  expect((await api('/api/v1/workspace', owner)).workspace_id).toBe(owner.workspaceId);
  result.gateway = owner;

  result.phase = 'create-session-and-main';
  const session = await api('/api/v1/sessions', owner, { title: 'Thread 规范引用验收' });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, owner);
  expect(main.session_id).toBe(session.session_id); result.sessionId = session.session_id; result.mainThreadId = main.thread_id;
  result.phase = 'create-product-history-for-child';
  await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: main.thread_id, interactions });
  const accepted = await api(`/api/v1/sessions/${session.session_id}/threads/${main.thread_id}/messages`, owner,
    { operation_id: newOperationId(), message: { content: '为 Thread 引用创建真实 fork 来源。' }, run: {} });
  expect(accepted.thread_id).toBe(main.thread_id);
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`, owner);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 45_000 }).toBe('completed');
  const point = await api(`/api/v1/sessions/${session.session_id}/fork-source-point`, owner);
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, owner, {
    idempotency_key: `thread-reference-child-${session.session_id}`, source_uri: point.source_uri,
    source_view_id: point.source_view_id, source_view_revision: point.source_view_revision,
    anchor_item_id: point.anchor_item_id, target_container_uri: point.child_container_uri,
    mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  expect(fork.session.session_id).toBe(session.session_id); expect(fork.thread_id).not.toBe(main.thread_id);
  result.childThreadId = fork.thread_id;

  const publicThreadUri = threadId => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/${session.session_id}/threads/${threadId}`;
  const internalThreadUri = threadId => `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${session.session_id}/${threadId}`;
  const groups = page.locator('.editor-group-container');
  const pane = threadId => page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${session.session_id}"][data-thread-id="${threadId}"]`);
  const copyFromRecentThread = async threadId => {
    const openedGroups = await groups.count(), openedTabs = await page.locator('.tabs-container .tab').count();
    result.phase = `copy-${threadId === main.thread_id ? 'main' : 'child'}`;
    const start = result.threadAddresses.length;
    await command('workbench.action.quickOpen', '');
    const quick = page.locator('.quick-input-widget');
    await expect(quick).toBeVisible(); await expect(quick).toContainText('最近打开');
    const row = quick.locator('.monaco-list-row').filter({ has: page.locator(`[data-quick-input-id="${internalThreadUri(threadId)}"]`) });
    await expect(row).toHaveCount(1);
    await row.hover();
    const button = row.getByRole('button', { name: '复制资源 URI', exact: true });
    await expect(button).toBeVisible();
    await button.click();
    await expect.poll(() => result.threadAddresses.length).toBe(start + 1);
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(
      `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/${session.session_id}/threads/${threadId}`);
    expect(result.threadAddresses.at(-1)).toMatchObject({ method: 'POST', body: {
      local_gateway_id: owner.gatewayId, resource_gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
      session_id: session.session_id, thread_id: threadId,
    } });
    await expect(quick).toBeVisible();
    await expect(groups).toHaveCount(openedGroups); await expect(page.locator('.tabs-container .tab')).toHaveCount(openedTabs);
    expect(await pane(threadId).count()).toBe(1);
    expect(result.apiRequests.filter(item => item.phase === result.phase
      && item.path.startsWith('/api/gateway/resource-addresses/')).map(item => item.path))
      .toEqual(['/api/gateway/resource-addresses/thread']);
    expect(result.resourceReads.filter(item => item.phase === result.phase)).toEqual([]);
    await page.keyboard.press('Escape'); await expect(quick).not.toBeVisible();
  };

  result.phase = 'open-main-and-child';
  await command('_workbench.open', publicThreadUri(main.thread_id));
  await expect(pane(main.thread_id).locator('textarea[data-testid="composer-input"]')).toBeEnabled({ timeout: 30_000 });
  await command('workbench.action.splitEditorRight');
  await command('_workbench.open', publicThreadUri(fork.thread_id));
  await expect(pane(fork.thread_id).locator('textarea[data-testid="composer-input"]')).toBeEnabled({ timeout: 30_000 });
  await copyFromRecentThread(main.thread_id);
  await copyFromRecentThread(fork.thread_id);
  result.mainAndChildReferences = true;

  result.phase = 'thread-composer-file-selection';
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await command('workbench.action.focusSecondEditorGroup');
  await pane(fork.thread_id).locator('textarea[data-testid="composer-input"]').focus();
  const beforeExecutionCount = result.executions.length, beforeTabs = await page.locator('.tabs-container .tab').count();
  const attachPending = command('boxteam.resource.attachToChat');
  await expect(quick).toBeVisible();
  await expect(input).toHaveAttribute('placeholder', '选择要加入发起 Thread 草稿的文件 · 不打开文件');
  await input.fill('thread-reference-source.txt');
  const file = quick.locator('.monaco-list-row').filter({ hasText: 'thread-reference-source.txt' });
  await expect(file).toHaveCount(1);
  await file.getByRole('checkbox').check();
  await input.press('Enter'); await attachPending;
  await expect(quick).not.toBeVisible();
  expect(result.fileAddresses.filter(item => item.phase === result.phase)).toHaveLength(1);
  await expect.poll(() => result.receipts.some(item => item.phase === result.phase
    && item.path === '/api/gateway/resource-addresses/parse' && item.receipt)).toBe(true);
  const fileAddress = result.receipts.find(item => item.phase === result.phase && item.path === '/api/gateway/resource-addresses/parse');
  expect(fileAddress.receipt.data.gateway_id).toBe(owner.gatewayId);
  await expect(pane(fork.thread_id).locator('textarea[data-testid="composer-input"]'))
    .toHaveValue(fileAddress.receipt.data.reference_uri);
  expect(result.resourceReads.filter(item => item.phase === result.phase)).toEqual([]);
  expect(await page.locator('.tabs-container .tab').count()).toBe(beforeTabs);
  expect(result.executions).toHaveLength(beforeExecutionCount);
  result.threadComposerKeptFilePicker = true;

  await Promise.all(pendingReceipts);
  expect(responseErrors).toEqual([]);
  expect(result.threadAddresses).toHaveLength(2);
  expect(result.threadAddresses.every(item => item.receipt.status === 200 && item.receipt.requestId === item.receipt.header)).toBe(true);
  const fileSelectionAddresses = result.fileAddresses.filter(item => item.phase === 'thread-composer-file-selection');
  expect(fileSelectionAddresses).toHaveLength(1);
  expect(fileSelectionAddresses[0].body).toMatchObject({ local_gateway_id: owner.gatewayId, resource_gateway_id: owner.gatewayId });
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (cause) {
  failure = cause; result.error = String(cause.stack ?? cause);
  if (page) await page.screenshot({ path: screenshotPath, fullPage: true }).catch(error => { result.screenshotError = String(error); });
} finally {
  try { await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n'); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '页面验收与结果保存均失败') : cause; }
  try { await run.close(); } catch (cause) { failure = failure ? new AggregateError([failure, cause], '页面验收与资源清理均失败') : cause; }
}
if (failure) throw failure;
