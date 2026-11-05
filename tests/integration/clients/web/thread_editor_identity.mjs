import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshotPath = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!baseUrl || !resultPath || !screenshotPath) throw new Error('Thread 身份验收缺少运行参数');
const scope = TestRunContext.fromTestFile('tests/integration/clients/web/test_thread_editor_identity.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], initialization: [],
  boundary: '真实 Gateway、完整工作台与托管后端；在产品保存的展示状态加入三条损坏记录，歧义场景将登记列表复制一条；会话、草稿和其余响应保持真实' };
let page;
let failure;

async function api(path, init = {}) {
  return page.evaluate(async ({ path, init }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ status: response.status, body }));
    return body;
  }, { path, init });
}
async function command(id, ...args) {
  return page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
}
async function open(uri) { await command('_workbench.open', uri, [undefined, { pinned: true }]); }

/** 仅损坏产品已保存的展示记录；会话、草稿和组布局先经真实入口生成。 */
async function verifyUnavailableRecovery(context, baseUrl, owner, threadSelector) {
  const future = { version: 3, title: '未知版本恢复记录', target: { ...owner, sessionId: 'future-must-not-be-requested' } };
  const missing = { version: 2, title: '缺失归属恢复记录', target: { sessionId: 'missing-must-not-be-requested' } };
  const malformed = '{"version":2,';
  const faults = [JSON.stringify(future), JSON.stringify(missing), malformed];
  const helper = await context.newPage();
  await helper.goto(`${baseUrl}/api/gateway/health`, { waitUntil: 'domcontentloaded' });
  const storage = async insert => helper.evaluate(async ({ insert, faults }) => {
    const read = request => new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    const rows = [];
    for (const { name } of await indexedDB.databases()) {
      if (!name?.startsWith('vscode-web-state-db-')) continue;
      const db = await read(indexedDB.open(name));
      try {
        const store = db.transaction('ItemTable').objectStore('ItemTable');
        const [keys, values] = await Promise.all([read(store.getAllKeys()), read(store.getAll())]);
        keys.forEach((key, index) => {
          if (key === 'memento/workbench.parts.editor') rows.push({ database: name, key, value: values[index] });
        });
      } finally { db.close(); }
    }
    if (rows.length !== 1) throw new Error(`恢复测试必须取得唯一原编辑器状态，实际 ${rows.length}`);
    const row = rows[0], state = JSON.parse(row.value), groups = [];
    const visit = node => {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node.editors) && node.editors.some(editor => editor.id === 'boxteam.editor.thread')) groups.push(node);
      for (const value of Object.values(node)) if (value && typeof value === 'object') visit(value);
    };
    visit(state);
    if (groups.length !== 2) throw new Error(`原产品分屏状态未保存，实际 ${groups.length}`);
    if (insert) {
      const group = groups[0];
      for (const value of faults) {
        group.mru.push(group.editors.length);
        group.editors.push({ id: 'boxteam.editor.thread', value });
      }
      const db = await read(indexedDB.open(row.database));
      try {
        await new Promise((resolve, reject) => {
          const tx = db.transaction('ItemTable', 'readwrite');
          tx.objectStore('ItemTable').put(JSON.stringify(state), row.key);
          tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
        });
      } finally { db.close(); }
    }
    return { database: row.database, key: row.key,
      records: groups.flatMap(group => group.editors.filter(editor => editor.id === 'boxteam.editor.thread').map(editor => editor.value)) };
  }, { insert, faults });
  const attach = current => current.on('pageerror', error => result.pageErrors.push(String(error)));
  const rejectedReads = [];
  const watch = current => current.on('request', request => {
    if (/future-must-not-be-requested|missing-must-not-be-requested/.test(request.url())) rejectedReads.push(request.url());
  });
  const baselineTabs = await page.locator('.tabs-container .tab').count();
  result.recoveryBaselineTabs = baselineTabs;
  const select = async label => {
    await command('workbench.action.quickOpen', `edt ${label}`);
    const quick = page.locator('.quick-input-widget');
    await quick.getByRole('option', { name: new RegExp(label) }).click();
    await expect(quick).toBeHidden();
  };
  await command('boxteam.flushViewState');
  await page.close();
  result.injectedRecoveryState = await storage(true);
  for (let round = 0; round < 2; round++) {
    page = await context.newPage(); attach(page); watch(page);
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.tabs-container .tab')).toHaveCount(baselineTabs + faults.length);
    await expect(page.locator(threadSelector).getByTestId('composer-input').first()).toHaveValue('刷新后保留的草稿');
    for (const [label, reason] of [
      ['未知版本恢复记录', '不支持的 Thread 编辑器恢复版本：3'],
      ['缺失归属恢复记录', 'Thread 编辑器恢复数据缺少精确 owner'],
      ['无法恢复的 Thread', 'Thread 编辑器恢复 JSON 损坏'],
    ]) {
      await select(label);
      await expect(page.getByText(reason, { exact: false }).first()).toBeVisible();
    }
    await command('boxteam.flushViewState');
    if (round === 0) {
      await page.close();
      const saved = await storage(false);
      const unavailable = saved.records.map(value => JSON.parse(value)).filter(value => Object.hasOwn(value, 'unavailable'));
      expect(unavailable.map(value => value.unavailable)).toEqual([future, missing, malformed]);
      result.preservedRecoveryState = saved;
    }
  }
  // 原生关闭只移除展示记录，不能删除原会话或其它组的草稿。
  for (const label of ['未知版本恢复记录', '缺失归属恢复记录', '无法恢复的 Thread']) {
    await select(label);
    await command('workbench.action.closeActiveEditor');
  }
  await expect(page.locator('.tabs-container .tab')).toHaveCount(baselineTabs);
  await expect(page.locator(threadSelector).getByTestId('composer-input').first()).toHaveValue('刷新后保留的草稿');
  expect((await api(`/api/v1/sessions/${owner.sessionId}`, { headers: {
    'X-BoxTeam-Workspace-Id': owner.workspaceId, 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId,
  } })).data.session_id).toBe(owner.sessionId);
  expect(rejectedReads).toEqual([]);
  result.unavailableRecordsPreserved = true; result.removalPreservedSessionAndDraft = true;
  await helper.close();
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  scope.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext();
  scope.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  result.phase = 'create-through-product-api';
  const health = await api('/api/gateway/health');
  const workspaces = await api('/api/gateway/workspaces');
  const active = workspaces.data.active_workspace;
  expect(active.gateway_id).toBe(health.data.gateway_id);
  const headers = { 'X-BoxTeam-Workspace-Id': active.workspace_id, 'X-BoxTeam-Target-Gateway-Id': active.gateway_id };
  const workspace = await api('/api/v1/workspace', { headers });
  expect(workspace.data.workspace_id).toBe(active.workspace_id);
  result.initialization = [health, workspaces, workspace].map(body => ({ requestId: body.request_id }));
  const session = (await api('/api/v1/sessions', { method: 'POST', headers,
    body: JSON.stringify({ title: '稳定身份恢复验收', agent_id: 'default' }) })).data;
  const main = (await api(`/api/v1/sessions/${session.session_id}/main-thread`, { headers })).data;
  expect(session.workspace_id).toBe(active.workspace_id);
  const owner = { gatewayId: active.gateway_id, workspaceId: active.workspace_id,
    sessionId: session.session_id, threadId: main.thread_id };
  result.owner = owner;
  const uri = `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${owner.sessionId}/${owner.threadId}`;
  const threadSelector = `section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`;
  const tabs = page.locator(`.tabs-container .tab[data-resource-name="${owner.threadId}"]`);
  const thread = page.locator(threadSelector);
  result.phase = 'open-and-deduplicate';
  await open(uri);
  await expect(thread.getByTestId('composer-input')).toBeVisible();
  await open(uri);
  await expect(tabs).toHaveCount(1);
  // 同一资源再由真实导航打开，不能因入口或标题不同创建另一标签。
  await page.getByRole('button', { name: '刷新资源树', exact: true }).click();
  await page.locator(`button[data-session-id="${owner.sessionId}"]`).dblclick();
  await expect(tabs).toHaveCount(1);
  result.stableOpenDeduplicated = true;

  result.phase = 'split-close-reopen-reload';
  await thread.getByTestId('composer-input').fill('刷新后保留的草稿');
  await thread.getByRole('button', { name: '切换会话资源', exact: true }).click();
  await command('workbench.action.splitEditorRight');
  await expect(tabs).toHaveCount(2);
  const groups = page.locator('.editor-group-container');
  await groups.nth(1).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true }).click();
  await expect(groups.nth(0).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await command('workbench.action.closeActiveEditor');
  await command('workbench.action.reopenClosedEditor');
  // 原版移除空组；当前组已有同一资源时，重开历史不会制造重复标签。
  await expect(tabs).toHaveCount(1);
  await command('workbench.action.splitEditorRight');
  await expect(tabs).toHaveCount(2);
  await groups.nth(1).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true }).click();
  await command('boxteam.flushViewState');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(tabs).toHaveCount(2);
  for (const group of [groups.nth(0), groups.nth(1)]) {
    await expect(group.locator(threadSelector).getByTestId('composer-input')).toHaveValue('刷新后保留的草稿');
  }
  await expect(groups.nth(0).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(groups.nth(1).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true })).toHaveAttribute('aria-expanded', 'false');
  result.splitAndReloadPreserved = true;

  result.phase = 'preserve-unavailable-records-with-valid-group';
  await verifyUnavailableRecovery(context, baseUrl, owner, threadSelector);

  result.phase = 'reject-unknown-gateway';
  const rejectedSession = 'session-must-not-be-requested';
  const reads = [];
  const observe = request => { if (new URL(request.url()).pathname.includes(rejectedSession)) reads.push(request.url()); };
  page.on('request', observe);
  await open(`boxteam-thread://gateway-unknown/${owner.workspaceId}/${rejectedSession}/${owner.threadId}`);
  await expect(page.getByText('目标 Gateway／UUID 缺少唯一已登记工作区', { exact: true }).first()).toBeVisible();
  expect(reads).toEqual([]);
  result.unknownGatewayRejected = true;
  await command('workbench.action.closeActiveEditor');

  result.phase = 'reject-ambiguous-registry';
  const duplicate = async route => {
    const response = await route.fetch();
    const body = await response.json();
    const source = body.data.items.find(item => item.gateway_id === owner.gatewayId && item.workspace_id === owner.workspaceId);
    expect(source).toBeDefined();
    body.data.items.push({ ...source });
    await route.fulfill({ response, json: body });
  };
  const registryUrl = url => url.pathname === '/api/gateway/workspaces';
  await page.route(registryUrl, duplicate);
  try {
    await open(`boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${rejectedSession}/${owner.threadId}`);
    await expect(page.getByText('目标 Gateway／UUID 缺少唯一已登记工作区', { exact: true }).first()).toBeVisible();
    expect(reads).toEqual([]);
    result.ambiguousRouteRejected = true;
  } finally {
    // 关闭原版错误编辑器仍可能触发导航重解；先等已接收的替身请求结束再释放页面。
    await page.unrouteAll({ behavior: 'wait' });
    page.off('request', observe);
  }
  await command('workbench.action.closeActiveEditor');
  await expect(page.locator(threadSelector).first().getByTestId('composer-input')).toBeVisible();
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) { failure = error; result.failure = String(error.stack ?? error); }
finally {
  const errors = failure ? [failure] : [];
  if (failure && page) { try { await page.screenshot({ path: screenshotPath, fullPage: true }); } catch (error) { errors.push(error); } }
  try { await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n'); } catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, 'Thread 稳定身份验收失败');
}
