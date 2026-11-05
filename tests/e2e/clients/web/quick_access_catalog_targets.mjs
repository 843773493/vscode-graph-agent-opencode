import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { searchRequest, searchReceipt } from '../../../support/workbench/search-observation.mjs';
import { verifyQuickAccessPagination } from './quick_access_pagination.mjs';
import { verifyResourceLinks } from './quick_access_resource_links.mjs';
import { gatewayWorkspaceKey } from '../../../../src/clients/web/src/api/workspaceIdentity.ts';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL, resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const otherWorkspace = process.env.BOXTEAM_CATALOG_OTHER_WORKSPACE;
const workspace = process.env.BOXTEAM_CATALOG_WORKSPACE;
if (!baseUrl || !resultPath || !otherWorkspace || !workspace) throw new Error('目录快速访问缺少运行参数');
const run = TestRunContext.fromTestFile('tests/e2e/clients/web/test_quick_access_catalog_targets.py');
const result = { classification: 'E2E', phase: 'prepare', pageErrors: [], receipts: [], searches: [], forbidden: [],
  businessRequests: [], notifications: [], consoleErrors: [], quickInputInteractions: [],
  boundary: '正式 Workbench、真实 Gateway 与托管工作区；Session、child 和 folder 全部由产品 API 创建，无业务替身' };
let page, failure;
const searchOwners = request => request.body.scope.mode === 'resume' ? request.body.scope.scope.owners : request.body.scope.owners;
async function api(path, owner, data) {
  const receipt = await page.evaluate(async ({ path, owner, data }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json',
        ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } : {}) },
      body: data === undefined ? undefined : JSON.stringify(data) });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return { status: response.status, body };
  }, { path, owner, data });
  result.receipts.push({ path, owner, status: receipt.status, requestId: receipt.body.request_id });
  return receipt.body.data;
}
async function command(id, ...args) {
  return page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
}
async function mutate(owner, kind, fields) {
  const snapshot = await api('/api/v1/session-catalog/snapshot', owner);
  const id = 'op_' + crypto.randomUUID().replaceAll('-', '');
  const intent = { client_operation_id: id, client_sequence: 1, base_catalog_revision: snapshot.catalog_revision, kind, ...fields };
  if (['move_node', 'rename_node'].includes(kind)) {
    const catalog = await api('/api/v1/session-catalog/export', owner);
    intent.expected_revision = catalog.items.find(node => node.node_id === fields.target_node_id).node_revision;
  }
  const accepted = await api('/api/v1/session-catalog/operations:enqueue', owner, { intents: [intent] });
  expect(accepted.receipts.some(item => item.operation_id === id)).toBe(true);
  let receipt;
  await expect.poll(async () => {
    receipt = (await api(`/api/v1/session-catalog/operations?operation_id=${id}`, owner)).items.find(item => item.operation_id === id);
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
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  await page.exposeFunction('recordCatalogNotification', text => result.notifications.push(text));
  await page.exposeFunction('recordCatalogQuickInputInteraction', interaction => {
    result.quickInputInteractions.push({ phase: result.phase, ...interaction });
  });
  await page.addInitScript(() => {
    new MutationObserver(() => {
      for (const node of document.querySelectorAll('.notifications-toasts .notification-list-item')) {
        const text = node.textContent?.trim();
        if (text && node.dataset.catalogRecorded !== text) {
          node.dataset.catalogRecorded = text;
          void window.recordCatalogNotification(text);
        }
      }
    }).observe(document, { childList: true, subtree: true, characterData: true });
    // 只观察原生 DOM 事件，区分定位器意图与鼠标实际命中的候选。
    const describe = element => element instanceof Element ? {
      tag: element.tagName, id: element.id, role: element.getAttribute('role'),
      classes: element.getAttribute('class'),
      pickId: element.closest('[data-quick-input-id]')?.getAttribute('data-quick-input-id')
        ?? element.querySelector('[data-quick-input-id]')?.getAttribute('data-quick-input-id'),
      text: element.textContent?.trim().slice(0, 300),
    } : null;
    for (const type of ['pointerdown', 'click']) document.addEventListener(type, event => {
      const target = event.composedPath().find(node => node instanceof Element);
      const quick = target?.closest('.quick-input-widget');
      if (!quick) return;
      let active = document.activeElement;
      while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
      void window.recordCatalogQuickInputInteraction({ type, button: event.button, time: performance.now(),
        target: describe(target), activeElement: describe(active),
        activeDescendant: quick.querySelector('input')?.getAttribute('aria-activedescendant'),
        focused: [...quick.querySelectorAll('.monaco-list-row.focused')].map(describe),
        selected: [...quick.querySelectorAll('.monaco-list-row.selected, .monaco-list-row[aria-selected="true"]')].map(describe),
      });
    }, true);
  });
  page.on('request', request => {
    const url = new URL(request.url());
    if (/^\/api\/v1\/sessions\//.test(url.pathname) || url.pathname === '/api/gateway/workspaces') {
      result.businessRequests.push({ phase: result.phase, method: request.method(), path: url.pathname, search: url.search,
        gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    }
    const observed = searchRequest(request);
    if (observed) result.searches.push({ ...observed, targetGatewayId: request.headers()['x-boxteam-target-gateway-id'] });
    if (request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(url.pathname)) result.forbidden.push(url.pathname);
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const ownerA = { gatewayId: health.gateway_id,
    workspaceId: JSON.parse(await readFile(join(workspace, '.boxteam/workspace-identity.json'), 'utf8')).workspace_id };
  expect(workspaces.active_workspace).toEqual({ gateway_id: ownerA.gatewayId, workspace_id: ownerA.workspaceId });
  const ownTargets = workspaces.items.filter(item => item.gateway_id === ownerA.gatewayId && item.workspace_id === ownerA.workspaceId);
  expect(ownTargets).toHaveLength(1); expect(ownTargets[0].connection_kind).toBe('local');
  const identity = await api('/api/v1/workspace', ownerA);
  expect(identity.workspace_id).toBe(ownerA.workspaceId);
  const registered = await api('/api/gateway/managed-workspaces', undefined, { root_path: otherWorkspace, name: '目录目标 B' });
  const ownerB = { gatewayId: ownerA.gatewayId,
    workspaceId: JSON.parse(await readFile(join(otherWorkspace, '.boxteam/workspace-identity.json'), 'utf8')).workspace_id };
  expect(ownerB.workspaceId).not.toBe(ownerA.workspaceId);
  const otherTargets = registered.items.filter(item => item.gateway_id === ownerB.gatewayId && item.workspace_id === ownerB.workspaceId);
  expect(otherTargets).toHaveLength(1); expect(otherTargets[0].name).toBe('目录目标 B');
  expect(registered.gateway_id).toBe(ownerB.gatewayId); expect(registered.connection_kind).toBe('local');
  const session = await api('/api/v1/sessions', ownerA, { title: 'catalogexact 同名目标' });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, ownerA);
  const point = await api(`/api/v1/sessions/${session.session_id}/fork-source-point?mode=full_rollout_copy&thread_id=${main.thread_id}`, ownerA);
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, ownerA, {
    idempotency_key: crypto.randomUUID(), source_uri: point.source_uri, source_view_id: point.source_view_id,
    source_view_revision: point.source_view_revision, mode: 'full_rollout_copy', anchor_mode: 'inclusive',
    target_container_uri: point.child_container_uri, creation_kind: 'new_child_thread', title: session.title,
    relationship: 'detached', resources: [],
  });
  expect(fork.session.session_id).toBe(session.session_id);
  expect(fork.thread_id).not.toBe(main.thread_id);
  result.owner = { ...ownerA, secondaryOwner: ownerB,
    sessionId: session.session_id, mainId: main.thread_id, childId: fork.thread_id };
  const parentA = (await mutate(ownerA, 'create_folder', { name: '原目录 A' })).created_node_id;
  const parentNew = (await mutate(ownerA, 'create_folder', { name: '新目录 A' })).created_node_id;
  const folder = (await mutate(ownerA, 'create_folder', { name: 'catalogfolder 同名目录', parent_node_id: parentA })).created_node_id;
  const folderB = (await mutate(ownerB, 'create_folder', { name: 'catalogfolder 同名目录' })).created_node_id;
  await mutate(ownerA, 'move_node', { target_node_id: session.session_id, parent_node_id: folder });
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
    result.openIntent = { id, pickId: await pick.getAttribute('data-quick-input-id'),
      pickText: await pick.innerText(), openThreads: await page.locator('section.boxteam-thread').evaluateAll(
        nodes => nodes.map(node => ({ sessionId: node.dataset.sessionId, threadId: node.dataset.threadId }))) };
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
  expect((await api(`/api/v1/sessions/${session.session_id}/main-thread`, ownerA)).thread_id).toBe(main.thread_id);
  result.sessionOpensMain = true;

  result.phase = 'folder-fresh-breadcrumb-and-original-navigation';
  const tabsBefore = await page.locator('.tabs-container .tab').count();
  const childrenBefore = (await api(`/api/v1/sessions/${session.session_id}/child-threads`, ownerA)).items.map(item => item.thread_id);
  await command('workbench.action.closeSidebar');
  await command('workbench.action.quickOpen', 'catalogfolder');
  const folderPick = quick.locator(`[data-quick-input-id="folder:${health.gateway_id}/${identity.workspace_id}/${folder}"]`);
  await expect(folderPick).toHaveCount(1);
  await expect(quick).not.toContainText(folderB);
  // 结果已显示后移动目标，原搜索 breadcrumb 此时失效。
  await mutate(ownerA, 'move_node', { target_node_id: folder, parent_node_id: parentNew });
  await folderPick.click();
  await expect(quick).not.toBeVisible();
  const row = page.getByTestId(`catalog-node-${gatewayWorkspaceKey(ownerA)}-${folder}`);
  await expect(row).toBeVisible();
  await expect(row.getByRole('button', { name: 'catalogfolder 同名目录', exact: true })).toBeFocused();
  await expect(page.getByTestId(`catalog-node-${gatewayWorkspaceKey(ownerA)}-${parentNew}`).locator('..')).toHaveAttribute('aria-expanded', 'true');
  expect(await page.locator('.tabs-container .tab').count()).toBe(tabsBefore);
  expect(result.searches.every(request => request.path === '/api/gateway/search'
    ? searchOwners(request).length === 1 && searchOwners(request)[0].gateway_id === ownerA.gatewayId
      && searchOwners(request)[0].workspace_id === ownerA.workspaceId
    : request.targetGatewayId === ownerA.gatewayId && request.route === ownerA.workspaceId)).toBe(true);
  expect(result.forbidden).toEqual([]);
  expect((await api(`/api/v1/sessions/${session.session_id}/child-threads`, ownerA)).items.map(item => item.thread_id)).toEqual(childrenBefore);
  result.folderUsesFreshNavigation = true;
  result.folderHasNoThreadOrExecution = true;

  result.phase = 'deleted-folder-is-not-opened';
  await command('workbench.action.quickOpen', 'catalogfolder');
  await expect(folderPick).toHaveCount(1);
  // 删除目录保留其中的 Session，旧候选不得改投 Session main 或另一个同名目录。
  await mutate(ownerA, 'move_node', { target_node_id: session.session_id, parent_node_id: null });
  await mutate(ownerA, 'delete_folder', { target_node_id: folder, recursive: false });
  await folderPick.click();
  await expect(page.locator('.notifications-toasts')).toContainText(/404|不存在|not found/);
  expect(await page.locator('.tabs-container .tab').count()).toBe(tabsBefore);
  expect(result.forbidden).toEqual([]);
  expect((await api(`/api/v1/sessions/${session.session_id}/main-thread`, ownerA)).thread_id).toBe(main.thread_id);
  result.deletedFolderRejected = true;
  await verifyQuickAccessPagination({ page, command, api, result, owner: result.owner,
    workspace: process.env.BOXTEAM_CATALOG_WORKSPACE });
  await verifyResourceLinks({ page, command, api, result, owner: result.owner });
  result.phase = 'dedicated-name-modes';
  const modeRequests = [];
  const modeReceipts = [];
  const queryMode = async (value, category, query) => {
    const [request] = await Promise.all([
      page.waitForRequest(request => {
        const observed = searchRequest(request);
        return observed?.path === '/api/gateway/search' && observed.query === query
          && observed.categories.length === 1 && observed.categories[0] === category
          && searchOwners(observed).length === 1 && searchOwners(observed)[0].gateway_id === ownerA.gatewayId
          && searchOwners(observed)[0].workspace_id === ownerA.workspaceId;
      }),
      quick.locator('input').first().fill(value),
    ]);
    const receipt = await searchReceipt(request);
    expect(receipt.status).toBe(200);
    expect(receipt.summary.branches.map(branch => branch.branch.category)).toEqual([category]);
    expect(receipt.summary.state).toBe('complete');
    modeReceipts.push({ category, query, requestId: receipt.requestId });
  };
  const observeMode = request => {
    const observed = searchRequest(request);
    if (observed?.path === '/api/gateway/search') modeRequests.push({ path: observed.path, categories: observed.categories, owners: searchOwners(observed) });
  };
  page.on('request', observeMode);
  try {
    await command('boxteam.quickAccess.sessions');
    await queryMode('session catalogexact', 'catalog', 'catalogexact');
    const sessionPick = quick.locator(`[data-quick-input-id="session:${health.gateway_id}/${identity.workspace_id}/${session.session_id}"]`);
    await expect(sessionPick).toHaveCount(1);
    await expect(quick.locator('[data-quick-input-id^="boxteam-thread:"]')).toHaveCount(0);
    await sessionPick.click();
    await expect(page.locator(`.editor-group-container.active section.boxteam-thread[data-thread-id="${main.thread_id}"]`)).toBeVisible();
    await command('boxteam.quickAccess.threads');
    // 最近历史也可包含同一 child；等本次查询回执后再接受，不能用旧历史替代查询验收。
    await queryMode(`thread ${fork.thread_id}`, 'thread', fork.thread_id);
    const exactChild = quick.locator(`[data-quick-input-id$="/${fork.thread_id}"]`);
    await expect(exactChild).toHaveCount(1);
    await expect(quick.locator('[data-quick-input-id^="session:"]')).toHaveCount(0);
    await exactChild.click();
    await expect(page.locator(`.editor-group-container.active section.boxteam-thread[data-thread-id="${fork.thread_id}"]`)).toBeVisible();
    expect(modeRequests).toEqual([
      { path: '/api/gateway/search', categories: ['catalog'], owners: [{ gateway_id: ownerA.gatewayId, workspace_id: ownerA.workspaceId }] },
      { path: '/api/gateway/search', categories: ['thread'], owners: [{ gateway_id: ownerA.gatewayId, workspace_id: ownerA.workspaceId }] },
    ]);
    result.nameModes = { requests: modeRequests, receipts: modeReceipts, exactOwners: true };
  } finally { page.off('request', observeMode); }
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
