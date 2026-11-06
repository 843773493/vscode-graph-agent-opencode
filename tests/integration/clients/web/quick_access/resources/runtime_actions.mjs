import { writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`缺少 ${name}`); return value; };
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_runtime_actions.py');
const result = { phase: 'initialize', pageErrors: [], requests: [], receipts: [], forbiddenWrites: [],
  boundary: '正式Workbench、真实Gateway与两个托管Backend；Session和运行资源由产品API创建；不替换HTTP或预写数据库；model_stream仅供启动' };
const pending = [], collectionErrors = [];
const phase = async value => { result.phase = value; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('运行资源页面Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1700, height: 1100 } }); page.setDefaultTimeout(20_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname, method = request.method(), headers = request.headers();
    if (path.startsWith('/api/v1/resources')) result.requests.push({ phase: result.phase, path, method,
      owner: { gateway_id: headers['x-boxteam-target-gateway-id'], workspace_id: headers['x-boxteam-workspace-id'] },
      body: method === 'POST' ? request.postDataJSON() : undefined });
    if (method === 'POST' && /\/(messages|jobs)(\/|$)/.test(path)) result.forbiddenWrites.push({ phase: result.phase, path });
  });
  page.on('requestfinished', request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/v1/resources')) return;
    const observedPhase = result.phase;
    pending.push((async () => { const response = await request.response(), body = await response.json();
      result.receipts.push({ phase: observedPhase, path, status: response.status(), request_id: body.request_id,
        header: response.headers()['x-request-id'], data: body.data, detail: body.detail });
    })().catch(cause => collectionErrors.push(String(cause))));
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', body: body === undefined ? undefined : JSON.stringify(body),
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) } });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, owner, body });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), registered = await api('/api/gateway/workspaces?check_health=false'), a = registered.active_workspace;
  expect(a.gateway_id).toBe(health.gateway_id); expect((await api('/api/v1/workspace', a)).workspace_id).toBe(a.workspace_id);
  await phase('prepare');
  const managed = await api('/api/gateway/managed-workspaces', undefined, { root_path: required('BOXTEAM_SOURCE_OTHER_WORKSPACE'), name: 'Runtime B' });
  const b = managed.items.find(item => item.name === 'Runtime B'); expect(b?.workspace_id).toBeTruthy();
  const sessions = await Promise.all([a, b].map(async owner => {
    const session = await api('/api/v1/sessions', owner, { title: '运行资源来源', agent_id: 'default' });
    const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, owner);
    return { session_id: session.session_id, thread_id: main.thread_id };
  }));
  const terminals = await Promise.all([a, b].map((owner, i) => api('/api/v1/resources/terminals', owner,
    { ...sessions[i], title: 'Needle 终端', retention_scope: 'workspace' })));
  const browserResource = await api('/api/v1/resources/browsers', a, { ...sessions[0], title: 'Needle 浏览器', retention_scope: 'thread', url: 'about:blank', viewport: { width: 640, height: 480 } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  await command('workbench.action.closeAllEditors');
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const camel = owner => ({ gatewayId: owner.gateway_id, workspaceId: owner.workspace_id });
  const rows = id => quick.locator('.monaco-list-row').filter({ hasText: id });
  const show = async (owner, id) => {
    await command('boxteam.quickAccess.resources', camel(owner));
    await expect(input).toHaveValue('resource '); await input.fill(`resource ${id}`); await expect(rows(id)).toBeVisible();
    await rows(id).click(); await expect(quick).toContainText('资源动作 ·');
  };
  const writes = value => result.requests.filter(item => item.phase === value && item.method === 'POST');
  const current = (owner, id) => api(`/api/v1/resources/${id}`, owner);
  const settle = async (id, action, trigger) => {
    const received = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/v1/resources/${id}/${action}`, { timeout: 30_000 });
    await trigger();
    const response = await received, payload = await response.json();
    expect(response.status()).toBe(200); expect(payload.request_id).toBe(response.headers()['x-request-id']);
    return payload.data;
  };
  await phase('browse'); await command('boxteam.quickAccess.resources', camel(a));
  await input.fill('resource Needle'); await expect(rows(terminals[0].resource_id)).toBeVisible();
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('Escape'); expect(writes(result.phase)).toEqual([]); result.browseOnly = true;
  await phase('cancel'); await show(a, terminals[0].resource_id); await page.keyboard.press('Escape');
  await expect(input).toHaveValue(`resource ${terminals[0].resource_id}`); await page.keyboard.press('Escape'); expect(writes(result.phase)).toEqual([]); result.cancelOnly = true;
  await phase('stop'); await show(a, terminals[0].resource_id);
  expect((await settle(terminals[0].resource_id, 'stop', () => quick.getByText('停止资源', { exact: true }).click())).status).toBe('terminated');
  expect((await current(a, terminals[0].resource_id)).status).toBe('terminated');
  expect(writes(result.phase)).toHaveLength(1); expect(writes(result.phase)[0].body).toEqual({ expected_revision: terminals[0].owner_revision });
  expect((await current(a, terminals[0].resource_id)).thread_associations).toEqual([sessions[0]]);
  expect((await current(b, terminals[1].resource_id)).status).toBe('running'); result.directStop = true;
  await phase('detach'); await show(a, terminals[0].resource_id); await quick.getByText('解除关联', { exact: true }).click();
  await expect(quick).toContainText('选择要解除的 Thread 关联');
  expect((await settle(terminals[0].resource_id, 'detach', () => quick.getByText(sessions[0].thread_id, { exact: true }).click())).thread_associations).toEqual([]);
  expect((await current(a, terminals[0].resource_id)).thread_associations).toEqual([]);
  expect((await current(a, terminals[0].resource_id)).status).toBe('terminated');
  expect(writes(result.phase)).toHaveLength(1); expect(writes(result.phase)[0].body.thread).toEqual(sessions[0]); result.detachedWithoutDelete = true;
  await phase('delete'); await show(a, terminals[0].resource_id); await quick.getByText('删除资源', { exact: true }).click();
  const dialog = page.locator('.monaco-dialog-box'); await expect(dialog).toBeVisible();
  expect((await settle(terminals[0].resource_id, 'delete', () => dialog.getByRole('button', { name: '删除资源', exact: true }).click())).status).toBe('deleted');
  expect((await current(a, terminals[0].resource_id)).status).toBe('deleted');
  expect(writes(result.phase)).toHaveLength(1); result.deletedSeparately = true;
  await phase('react'); await command('boxteam.browser.focus');
  const panel = page.locator('.native-workspace-panel[data-panel-kind="browser"]:visible');
  const browserRows = panel.locator('.resource-tree-item[data-resource-kind="browser"]');
  await expect(browserRows).toHaveCount(1);
  // 原资源ID位于展开详情，先只展开展示，再核精确ID后执行管理动作。
  await browserRows.locator('.resource-tree-chevron').click();
  const row = browserRows.filter({ has: page.locator(`[title="${browserResource.resource_id}"]`) });
  await expect(row).toBeVisible();
  expect((await settle(browserResource.resource_id, 'promote', () => row.getByRole('button', { name: '保留到工作区', exact: true }).click())).retention_scope).toBe('workspace');
  expect((await current(a, browserResource.resource_id)).retention_scope).toBe('workspace');
  expect(writes(result.phase)).toHaveLength(1); expect(writes(result.phase)[0].path).toBe(`/api/v1/resources/${browserResource.resource_id}/promote`); result.originalReact = true;
  await phase('conflict'); const before = await current(a, browserResource.resource_id);
  await show(a, browserResource.resource_id);
  await api(`/api/v1/resources/${browserResource.resource_id}/stop`, a, { expected_revision: before.owner_revision });
  await quick.getByText('停止资源', { exact: true }).click();
  await expect.poll(() => result.receipts.some(item => item.phase === 'conflict' && item.status === 409)).toBe(true);
  expect(writes(result.phase)).toHaveLength(2); expect(writes(result.phase)[1].body.expected_revision).toBe(before.owner_revision);
  await expect(page.locator('.notification-list-item-message').filter({ hasText: /版本|revision/ }).first()).toBeVisible(); result.revisionConflict = true;
  await phase('owner-B'); await show(b, terminals[1].resource_id);
  expect((await settle(terminals[1].resource_id, 'stop', () => quick.getByText('停止资源', { exact: true }).click())).status).toBe('terminated');
  expect((await current(b, terminals[1].resource_id)).status).toBe('terminated');
  expect(writes(result.phase)).toHaveLength(1); expect(writes(result.phase)[0].owner).toEqual({ gateway_id: b.gateway_id, workspace_id: b.workspace_id });
  expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual(a); result.ownerB = true;
  await Promise.all(pending); expect(collectionErrors).toEqual([]); expect(result.pageErrors).toEqual([]); expect(result.forbiddenWrites).toEqual([]);
  expect(result.receipts.every(item => item.request_id && item.header === item.request_id)).toBe(true);
  await phase('complete');
} catch (cause) {
  failure = cause; result.error = cause.stack ?? String(cause);
  if (page) await page.screenshot({ path: join(dirname(output), 'runtime-actions-failure.png'), fullPage: false });
} finally {
  await Promise.all(pending); await writeFile(output, JSON.stringify(result, null, 2) + '\n'); await run.close();
}
if (failure) throw failure;
