import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_CONNECTION_MANAGEMENT_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], unexpectedCalls: [], requests: [], route_stubs: [] };
let browser, context, page;
const pending = new Set(), cleanupErrors = [];
let failure;

try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  context = await browser.newContext({ viewport: { width: 1450, height: 1000 } });
  page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    if (/tool-approvals|\/messages(?:$|\?)|\/chat\/completions|\/responses(?:$|\?)/.test(new URL(request.url()).pathname)) {
      result.unexpectedCalls.push(request.url());
    }
  });
  page.on('response', response => {
    const pathname = new URL(response.url()).pathname;
    if (pathname !== '/api/gateway/management/gateways' && !pathname.startsWith('/api/gateway/remote-gateways')) return;
    const operation = (async () => {
      const envelope = await response.json(), requestId = response.headers()['x-request-id'];
      assert(requestId);
      if (envelope.request_id) assert.equal(envelope.request_id, requestId);
      result.requests.push({ path: pathname, method: response.request().method(), status: response.status(), requestId });
    })();
    pending.add(operation);
    void operation.finally(() => pending.delete(operation)).catch(error => cleanupErrors.push(error.stack));
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (url, method = 'GET', expectedStatus = 200) => page.evaluate(async ({ url, method, expectedStatus }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { method, headers: { 'X-Local-Token': credential.token } });
    const envelope = await response.json(), requestId = response.headers.get('X-Request-ID');
    if (response.status !== expectedStatus || !requestId || (envelope.request_id && envelope.request_id !== requestId)) {
      throw new Error(`${url}: ${response.status} ${JSON.stringify(envelope)}`);
    }
    return { status: response.status, requestId, data: envelope.data };
  }, { url, method, expectedStatus });
  const control = page.locator('.settings-editor:visible');
  const close = async () => {
    await command('workbench.action.closeActiveEditor');
    await expect(page.locator('.settings-management-view:visible')).toHaveCount(0);
  };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  const localWorkspace = page.getByTestId('workspace-node-' + JSON.stringify([fixture.gatewayA, fixture.workspaceId]));
  await expect(localWorkspace).toBeVisible({ timeout: 30000 });
  result.initialization = [];
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push(await api(url));
  assert.equal(result.initialization[0].data.gateway_id, fixture.gatewayA);
  assert.deepEqual(result.initialization[1].data.active_workspace, { gateway_id: fixture.gatewayA, workspace_id: fixture.workspaceId });
  assert.equal(result.initialization[2].data.workspace_id, fixture.workspaceId);

  result.phase = 'config-owner-and-first-open';
  const firstStarted = performance.now();
  await command('boxteam.gateway.connections');
  const configRow = control.getByRole('article', { name: '配置连接 B', exact: true });
  await expect(configRow).toContainText(fixture.configConnectionId);
  await expect(configRow).toContainText(fixture.gatewayB);
  result.firstAvailableMs = performance.now() - firstStarted;
  assert(result.firstAvailableMs <= 3000, `连接目录首开 ${result.firstAvailableMs}ms`);
  await expect(control).toContainText(fixture.gatewayA);
  await expect(configRow).toContainText('来源：Gateway 配置');
  await expect(configRow.getByRole('button', { name: '编辑显示名', exact: true })).toHaveCount(0);
  await expect(configRow.getByRole('button', { name: '移除连接', exact: true })).toHaveCount(0);
  result.configManualDelete = await api('/api/gateway/remote-gateways/' + fixture.configConnectionId, 'DELETE', 403);
  await configRow.getByRole('button', { name: '打开 Gateway 来源设置', exact: true }).click();
  await expect(control.locator('.settings-management-view')).toBeHidden();
  const settingsHeader = page.locator('.settings-context-label:visible');
  await expect(settingsHeader).toContainText(fixture.gatewayA);
  await expect(page.locator('.settings-category-context:visible select[aria-label="配置范围"] option:checked')).toHaveText('Gateway 设置');
  await settingsHeader.locator('summary').click();
  await expect(settingsHeader.getByRole('combobox', { name: '配置来源', exact: true }).locator('option:checked')).toHaveText('目标设置');
  result.configSettingsOwner = fixture.gatewayA;

  result.phase = 'manual-create-cancel';
  await command('boxteam.gateway.connections');
  const directoryCount = () => result.requests.filter(item => item.method === 'POST' && item.path === '/api/gateway/remote-gateways').length;
  await control.getByRole('button', { name: '添加 SSH 连接', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '添加 SSH 连接', exact: true });
  const manualDialog = page.getByRole('dialog', { name: '手动添加 SSH 连接', exact: true });
  await dialog.getByRole('button', { name: '手动添加', exact: true }).click();
  await manualDialog.getByLabel('连接名称', { exact: true }).fill('取消连接');
  await manualDialog.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(manualDialog).toBeHidden();
  assert.equal(directoryCount(), 0);

  const create = async () => {
    await control.getByRole('button', { name: '添加 SSH 连接', exact: true }).click();
    await dialog.getByRole('button', { name: '手动添加', exact: true }).click();
    for (const [label, value] of [['连接名称', '手动连接 B'], ['SSH 主机', '127.0.0.1'], ['SSH 端口', '22'],
      ['用户名', 'connection-manual'], ['私钥路径', fixture.privateKeyPath], ['远程 Gateway 端口', String(fixture.remoteGatewayPort)]]) {
      await manualDialog.getByLabel(label, { exact: true }).fill(value);
    }
    const receipt = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/gateway/remote-gateways');
    await manualDialog.getByRole('button', { name: '添加', exact: true }).click();
    const response = await receipt;
    assert.equal(response.status(), 200, await response.text());
    await expect(manualDialog).toBeHidden();
    await expect(control.getByRole('article', { name: '手动连接 B', exact: true })).toBeVisible();
    const directory = (await api('/api/gateway/management/gateways')).data;
    const target = directory.items.find(item => item.source_owner === 'manual');
    assert(target && target.gateway_id === fixture.gatewayB && target.remote_gateway_port === fixture.remoteGatewayPort);
    return target.gateway_connection_id;
  };
  result.phase = 'manual-create-and-edit';
  const connectionId = await create();
  result.connectionId = connectionId;
  let row = control.getByRole('article', { name: '手动连接 B', exact: true });
  await expect(row).toContainText('来源：手动登记');
  await row.locator('summary').click();
  await expect(row.locator('li')).toContainText(fixture.workspaceId);
  await row.getByRole('button', { name: '编辑显示名', exact: true }).click();
  await row.getByLabel('连接名称', { exact: true }).fill('不会保存的名称');
  await row.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(directoryCount(), 1);
  await row.getByRole('button', { name: '编辑显示名', exact: true }).click();
  await row.getByLabel('连接名称', { exact: true }).fill('改名连接 B');
  const renamed = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/gateway/remote-gateways');
  await row.getByRole('button', { name: '保存显示名并重新连接', exact: true }).click();
  const renameResponse = await renamed;
  assert.equal(renameResponse.status(), 200, await renameResponse.text());
  assert.deepEqual(renameResponse.request().postDataJSON(), { connection_id: connectionId, name: '改名连接 B', remote_gateway_port: fixture.remoteGatewayPort });
  row = control.getByRole('article', { name: '改名连接 B', exact: true });
  await expect(row).toContainText(connectionId);
  await expect(row.getByRole('button', { name: '编辑显示名', exact: true })).toBeEnabled();
  const renamedTarget = (await api('/api/gateway/management/gateways')).data.items.find(item => item.gateway_connection_id === connectionId);
  assert.equal(renamedTarget.name, '改名连接 B');
  assert.equal(renamedTarget.source_owner, 'manual');

  result.phase = 'reconnect-cancel-and-commit';
  await row.getByRole('button', { name: '重新连接', exact: true }).click();
  const confirm = page.locator('.workspace-action-dialog');
  await expect(confirm).toContainText(fixture.gatewayB);
  await confirm.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(result.requests.filter(item => item.path.endsWith('/reconnect')).length, 0);
  await row.getByRole('button', { name: '重新连接', exact: true }).click();
  const reconnected = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/${connectionId}/reconnect`));
  await confirm.getByRole('button', { name: '重新连接', exact: true }).click();
  assert.equal((await reconnected).status(), 200);
  await expect(row.getByRole('button', { name: '移除连接', exact: true })).toBeEnabled();

  result.phase = 'remove-cancel-and-real-conflict';
  await row.getByRole('button', { name: '移除连接', exact: true }).click();
  await expect(confirm).toContainText('不会停止远端机器、删除远端工作区、会话或磁盘数据');
  await confirm.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(result.requests.filter(item => item.method === 'DELETE' && item.path.endsWith(connectionId)).length, 0);
  await row.getByRole('button', { name: '移除连接', exact: true }).click();
  await expect(confirm).toBeVisible();
  await api('/api/gateway/remote-gateways/' + connectionId, 'DELETE');
  const rejected = page.waitForResponse(response => response.request().method() === 'DELETE' && new URL(response.url()).pathname.endsWith(connectionId) && response.status() === 404);
  await confirm.getByRole('button', { name: '移除连接', exact: true }).click();
  const conflict = await rejected;
  await expect(control.getByRole('alert')).toContainText(conflict.headers()['x-request-id']);
  await expect(control.getByRole('alert')).toContainText(fixture.gatewayA);
  await expect(control.getByRole('alert')).toContainText(fixture.gatewayB);
  await expect(control.getByRole('alert')).toContainText(connectionId);
  await expect(row).toHaveCount(0);
  result.conflict = { requestId: conflict.headers()['x-request-id'], status: conflict.status() };
  await control.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(control.getByRole('alert')).toHaveCount(0);

  result.phase = 'manual-remove';
  assert.equal(await create(), connectionId, '同一SSH入口应沿原稳定连接身份重新登记');
  row = control.getByRole('article', { name: '手动连接 B', exact: true });
  await row.getByRole('button', { name: '移除连接', exact: true }).click();
  const removed = page.waitForResponse(response => response.request().method() === 'DELETE' && new URL(response.url()).pathname.endsWith(connectionId));
  await confirm.getByRole('button', { name: '移除连接', exact: true }).click();
  assert.equal((await removed).status(), 200);
  await expect(row).toHaveCount(0);
  await expect(configRow).toBeVisible();
  assert.deepEqual((await api('/api/gateway/workspaces')).data.active_workspace, { gateway_id: fixture.gatewayA, workspace_id: fixture.workspaceId });

  result.phase = 'representative-warm-directory';
  // 本次改变连接目录投影，唯一代表列表使用真实目录；没有新增详情API。
  result.apiSamples = await page.evaluate(async () => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token, samples = [];
    for (let index = 0; index < 21; index++) {
      const started = performance.now();
      const response = await fetch('/api/gateway/management/gateways', { headers: { 'X-Local-Token': token } });
      const text = await response.text(), elapsedMs = performance.now() - started, body = JSON.parse(text);
      if (!response.ok || response.headers.get('X-Request-ID') !== body.request_id) throw new Error(text);
      samples.push({ elapsedMs, requestId: body.request_id, bytes: new TextEncoder().encode(text).length, count: body.data.items.length });
    }
    return samples;
  });
  const warm = result.apiSamples.slice(1).map(sample => sample.elapsedMs).sort((a, b) => a - b);
  result.apiBudget = { p50: warm[Math.ceil(warm.length * .5) - 1], p95: warm[Math.ceil(warm.length * .95) - 1], max: warm.at(-1) };
  assert(result.apiBudget.p95 <= 500 && result.apiBudget.max <= 1500, JSON.stringify(result.apiBudget));
  await page.screenshot({ path: path.join(artifacts, 'gateway-connections.png') });
  await close();
  await Promise.all([...pending]);
  const reads = result.requests.length;
  await page.waitForTimeout(600);
  assert.equal(result.requests.length, reads, '关闭连接页后不能继续轮询目录');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await expect(localWorkspace).toBeVisible({ timeout: 30000 });
  await command('boxteam.gateway.connections');
  await expect(configRow).toBeVisible();
  await expect(control.getByRole('article', { name: '手动连接 B', exact: true })).toHaveCount(0);
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.unexpectedCalls, []);
  result.phase = 'completed';
} catch (error) {
  failure = error;
  result.error = error.stack ?? String(error);
} finally {
  await Promise.allSettled([...pending]);
  if (page && failure) {
    try { await page.screenshot({ path: path.join(artifacts, 'failure.png') }); }
    catch (error) { cleanupErrors.push(error.stack ?? String(error)); }
  }
  try { await context?.close(); } catch (error) { cleanupErrors.push(error.stack ?? String(error)); }
  try { await browser?.close(); } catch (error) { cleanupErrors.push(error.stack ?? String(error)); }
  if (cleanupErrors.length) result.cleanupErrors = cleanupErrors;
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n');
  if (failure || cleanupErrors.length) throw new AggregateError([...(failure ? [failure] : []), ...cleanupErrors], 'Gateway连接管理验收失败');
}
