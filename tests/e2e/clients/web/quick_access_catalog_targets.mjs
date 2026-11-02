import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL, resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const otherWorkspace = process.env.BOXTEAM_CATALOG_OTHER_WORKSPACE;
if (!baseUrl || !resultPath || !otherWorkspace) throw new Error('目录快速访问缺少运行参数');
const run = TestRunContext.fromTestFile('tests/e2e/clients/web/test_quick_access_catalog_targets.py');
const result = { classification: 'E2E', phase: 'prepare', pageErrors: [], receipts: [], searches: [], forbidden: [],
  boundary: '正式 Workbench、真实 Gateway 与托管工作区；Session、child 和 folder 全部由产品 API 创建，无业务替身' };
let page, failure;
async function api(path, route, data) {
  const receipt = await page.evaluate(async ({ path, route, data }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(route ? { 'X-BoxTeam-Workspace-Id': route } : {}) },
      body: data === undefined ? undefined : JSON.stringify(data) });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return { status: response.status, body };
  }, { path, route, data });
  result.receipts.push({ path, route, status: receipt.status, requestId: receipt.body.request_id });
  return receipt.body.data;
}
async function command(id, ...args) {
  return page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
}
async function mutate(route, kind, fields) {
  const snapshot = await api('/api/v1/session-catalog/snapshot', route);
  const id = 'op_' + crypto.randomUUID().replaceAll('-', '');
  const intent = { client_operation_id: id, client_sequence: 1, base_catalog_revision: snapshot.catalog_revision, kind, ...fields };
  if (['move_node', 'rename_node'].includes(kind)) {
    const catalog = await api('/api/v1/session-catalog/export', route);
    intent.expected_revision = catalog.items.find(node => node.node_id === fields.target_node_id).node_revision;
  }
  const accepted = await api('/api/v1/session-catalog/operations:enqueue', route, { intents: [intent] });
  expect(accepted.receipts.some(item => item.operation_id === id)).toBe(true);
  let receipt;
  await expect.poll(async () => {
    receipt = (await api(`/api/v1/session-catalog/operations?operation_id=${id}`, route)).items.find(item => item.operation_id === id);
    return receipt?.state;
  }, { timeout: 30_000 }).toBe('committed');
  return receipt;
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const url = new URL(request.url());
    if (['/api/v1/session-catalog/search', '/api/v1/session-threads/search'].includes(url.pathname)) {
      result.searches.push({ path: url.pathname, query: url.searchParams.get('query'), route: request.headers()['x-boxteam-workspace-id'] });
    }
    if (request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(url.pathname)) result.forbidden.push(url.pathname);
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const routeA = workspaces.active_workspace_id, identity = await api('/api/v1/workspace', routeA);
  const registered = await api('/api/gateway/managed-workspaces', undefined, { root_path: otherWorkspace, name: '目录目标 B' });
  const routeB = registered.items.find(item => item.name === '目录目标 B').workspace_id;
  const session = await api('/api/v1/sessions', routeA, { title: 'catalogexact 同名目标' });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, routeA);
  const point = await api(`/api/v1/sessions/${session.session_id}/fork-source-point?mode=full_rollout_copy&thread_id=${main.thread_id}`, routeA);
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, routeA, {
    idempotency_key: crypto.randomUUID(), source_uri: point.source_uri, source_view_id: point.source_view_id,
    source_view_revision: point.source_view_revision, mode: 'full_rollout_copy', anchor_mode: 'inclusive',
    target_container_uri: point.child_container_uri, creation_kind: 'new_child_thread', title: session.title,
    relationship: 'detached', resources: [],
  });
  expect(fork.session.session_id).toBe(session.session_id);
  expect(fork.thread_id).not.toBe(main.thread_id);
  result.owner = { gatewayId: health.gateway_id, workspaceId: identity.workspace_id, routeA, routeB,
    sessionId: session.session_id, mainId: main.thread_id, childId: fork.thread_id };
  const parentA = (await mutate(routeA, 'create_folder', { name: '原目录 A' })).created_node_id;
  const parentNew = (await mutate(routeA, 'create_folder', { name: '新目录 A' })).created_node_id;
  const folder = (await mutate(routeA, 'create_folder', { name: 'catalogfolder 同名目录', parent_node_id: parentA })).created_node_id;
  const folderB = (await mutate(routeB, 'create_folder', { name: 'catalogfolder 同名目录' })).created_node_id;
  await mutate(routeA, 'move_node', { target_node_id: session.session_id, parent_node_id: folder });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });

  result.phase = 'session-main-and-unopened-child';
  const quick = page.locator('.quick-input-widget');
  await command('workbench.action.quickOpen', 'catalogexact');
  const child = quick.locator(`[data-quick-input-id$="/${fork.thread_id}"]`);
  await expect(child).toHaveCount(1);
  await expect(quick.locator(`[data-quick-input-id$="/${main.thread_id}"]`)).toHaveCount(1);
  await expect(quick.locator(`[data-quick-input-id="session:${health.gateway_id}/${identity.workspace_id}/${session.session_id}"]`)).toHaveCount(1);
  expect(await page.locator(`section.boxteam-thread[data-thread-id="${fork.thread_id}"]`).count()).toBe(0);
  const openThread = async (pick, id) => {
    const started = performance.now();
    const [response] = await Promise.all([page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === `/api/v1/sessions/${session.session_id}/bootstrap` && url.searchParams.get('thread_id') === id;
    }, { timeout: 30_000 }), pick.click()]);
    const body = await response.json();
    expect(response.status()).toBe(200);
    expect(body.request_id).toBe(response.headers()['x-request-id']);
    result.receipts.push({ path: new URL(response.url()).pathname, threadId: id, requestId: body.request_id,
      elapsedMs: performance.now() - started });
  };
  await openThread(child, fork.thread_id);
  const thread = id => page.locator(`section.boxteam-thread[data-workspace-id="${identity.workspace_id}"][data-session-id="${session.session_id}"][data-thread-id="${id}"]`);
  await expect(thread(fork.thread_id).getByTestId('composer-input')).toBeVisible();
  result.unopenedChildKeepsIdentity = true;
  await command('workbench.action.quickOpen', 'catalogexact');
  await openThread(quick.locator(`[data-quick-input-id="session:${health.gateway_id}/${identity.workspace_id}/${session.session_id}"]`), main.thread_id);
  await expect(thread(main.thread_id).getByTestId('composer-input')).toBeVisible();
  expect((await api(`/api/v1/sessions/${session.session_id}/main-thread`, routeA)).thread_id).toBe(main.thread_id);
  result.sessionOpensMain = true;

  result.phase = 'folder-fresh-breadcrumb-and-original-navigation';
  const tabsBefore = await page.locator('.tabs-container .tab').count();
  const childrenBefore = (await api(`/api/v1/sessions/${session.session_id}/child-threads`, routeA)).items.map(item => item.thread_id);
  await command('workbench.action.closeSidebar');
  await command('workbench.action.quickOpen', 'catalogfolder');
  const folderPick = quick.locator(`[data-quick-input-id="folder:${health.gateway_id}/${identity.workspace_id}/${folder}"]`);
  await expect(folderPick).toHaveCount(1);
  await expect(quick).not.toContainText(folderB);
  // 结果已显示后移动目标，原搜索 breadcrumb 此时失效。
  await mutate(routeA, 'move_node', { target_node_id: folder, parent_node_id: parentNew });
  await folderPick.click();
  await expect(quick).not.toBeVisible();
  const row = page.getByTestId(`catalog-node-${routeA}-${folder}`);
  await expect(row).toBeVisible();
  await expect(row.getByRole('button', { name: 'catalogfolder 同名目录', exact: true })).toBeFocused();
  await expect(page.getByTestId(`catalog-node-${routeA}-${parentNew}`).locator('..')).toHaveAttribute('aria-expanded', 'true');
  expect(await page.locator('.tabs-container .tab').count()).toBe(tabsBefore);
  expect(result.searches.every(request => request.route === routeA)).toBe(true);
  expect(result.forbidden).toEqual([]);
  expect((await api(`/api/v1/sessions/${session.session_id}/child-threads`, routeA)).items.map(item => item.thread_id)).toEqual(childrenBefore);
  result.folderUsesFreshNavigation = true;
  result.folderHasNoThreadOrExecution = true;

  result.phase = 'deleted-folder-is-not-opened';
  await command('workbench.action.quickOpen', 'catalogfolder');
  await expect(folderPick).toHaveCount(1);
  // 删除目录保留其中的 Session，旧候选不得改投 Session main 或另一个同名目录。
  await mutate(routeA, 'move_node', { target_node_id: session.session_id, parent_node_id: null });
  await mutate(routeA, 'delete_folder', { target_node_id: folder, recursive: false });
  await folderPick.click();
  await expect(page.locator('.notifications-toasts')).toContainText(/404|不存在|not found/);
  expect(await page.locator('.tabs-container .tab').count()).toBe(tabsBefore);
  expect(result.forbidden).toEqual([]);
  expect((await api(`/api/v1/sessions/${session.session_id}/main-thread`, routeA)).thread_id).toBe(main.thread_id);
  result.deletedFolderRejected = true;
  result.phase = 'complete';
  expect(result.pageErrors).toEqual([]);
} catch (error) {
  failure = error;
  result.failure = error instanceof Error ? error.stack : String(error);
  if (page) {
    try { await page.screenshot({ path: resultPath.replace(/\.json$/, '-failure.png'), fullPage: true }); }
    catch (diagnostic) { failure = new AggregateError([failure, diagnostic], '目录验收与截图均失败'); }
  }
} finally {
  try { await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n'); }
  catch (diagnostic) { failure = failure ? new AggregateError([failure, diagnostic], '目录验收与结果保存均失败') : diagnostic; }
  try { await run.close(); }
  catch (cleanup) { failure = failure ? new AggregateError([failure, cleanup], '目录验收与清理均失败') : cleanup; }
}
if (failure) throw failure;
