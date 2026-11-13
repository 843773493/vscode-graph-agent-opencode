import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_GATEWAY_MANAGEMENT_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], unexpectedCalls: [], requests: [],
  writes: { create: 0, revoke: 0 }, initialization: [], apiSamples: [] };
const tokens = [];
const safe = value => tokens.reduce((text, token) => text.replaceAll(token, '[device-secret-redacted]'), String(value));
let browser, context, page;
const pending = new Set();
const cleanupErrors = [];

try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  context = await browser.newContext({ viewport: { width: 1360, height: 920 } });
  page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(safe(error.message)));
  page.on('request', request => {
    const url = new URL(request.url());
    if (/tool-approvals|\/messages(?:$|\?)|\/chat\/completions|\/responses(?:$|\?)/.test(url.pathname)) result.unexpectedCalls.push(url.pathname);
    if (url.pathname === '/api/gateway/device-connections' && request.method() === 'POST') result.writes.create++;
    if (url.pathname.startsWith('/api/gateway/device-connections/') && request.method() === 'DELETE') result.writes.revoke++;
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (url.pathname !== '/api/gateway/inbound-access' && !url.pathname.startsWith('/api/gateway/device-connections')) return;
    const operation = (async () => {
      const body = await response.json();
      const requestId = response.headers()['x-request-id'];
      assert(requestId);
      assert.equal(body.request_id, requestId);
      result.requests.push({ path: url.pathname, method: response.request().method(), status: response.status(), requestId });
    })();
    pending.add(operation);
    void operation.finally(() => pending.delete(operation)).catch(error => cleanupErrors.push(safe(error.stack)));
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (url, method = 'GET') => page.evaluate(async ({ url, method, owner }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { method, headers: { 'X-Local-Token': credential.token,
      'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return { status: response.status, requestId: body.request_id, data: body.data };
  }, { url, method, owner: fixture });
  const control = page.locator('.gateway-control-shell');
  const closeControl = async () => {
    await page.getByRole('button', { name: '关闭 Gateway 控制中心', exact: true }).click();
    await expect(control).toBeHidden();
  };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push(await api(url));
  assert.equal(result.initialization[0].data.gateway_id, fixture.gatewayId);
  assert.equal(result.initialization[1].data.active_workspace.workspace_id, fixture.workspaceId);
  assert.equal(result.initialization[2].data.workspace_id, fixture.workspaceId);
  const originalWorkspaceIds = result.initialization[1].data.items.map(item => item.workspace_id).sort();

  result.phase = 'command-menu-and-demand-loading';
  const otherGatewayId = fixture.gatewayId.slice(0, -1) + (fixture.gatewayId.endsWith('0') ? '1' : '0');
  await assert.rejects(command('boxteam.gateway.credentials', otherGatewayId), /Gateway 或访问凭据已改变/);
  await expect(control).toBeHidden();
  await command('boxteam.gateway.connections');
  await expect(control.getByRole('heading', { name: '连接管理', exact: true })).toBeVisible();
  assert.equal(result.requests.length, 0, '连接页不应提前读取设备或入站授权');
  await closeControl();
  await page.getByRole('button', { name: '管理', exact: true }).click();
  await expect(page.getByRole('menuitem', { name: '连接与凭据', exact: true })).toBeVisible();
  const firstStarted = performance.now();
  await page.getByRole('menuitem', { name: '连接与凭据', exact: true }).click();
  await expect(control.getByLabel('入站访问范围', { exact: true })).toContainText(fixture.gatewayId);
  await expect(control.getByText('暂无已授权的外部设备', { exact: true })).toBeVisible();
  result.firstAvailableMs = performance.now() - firstStarted;
  assert(result.firstAvailableMs <= 3000, `设备管理首次可用 ${result.firstAvailableMs}ms`);
  await expect(control.getByText('凭据管理范围', { exact: true })).toBeVisible();
  await expect(control).toContainText('Gateway 之间的入站授权在此只读');

  result.phase = 'device-create-cancel';
  await control.getByRole('button', { name: '添加设备', exact: true }).click();
  const deviceDialog = page.getByRole('dialog', { name: '连接外部设备', exact: true });
  await expect(deviceDialog.getByLabel('设备名称', { exact: true })).toBeVisible();
  await deviceDialog.getByLabel('设备名称', { exact: true }).fill('取消的设备');
  await deviceDialog.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(deviceDialog).toBeHidden();
  assert.equal(result.writes.create, 0);

  const createDevice = async name => {
    await control.getByRole('button', { name: '添加设备', exact: true }).click();
    await deviceDialog.getByLabel('设备名称', { exact: true }).fill(name);
    await deviceDialog.getByRole('button', { name: '手动地址', exact: false }).click();
    await deviceDialog.getByLabel('手动输入访问地址', { exact: true }).fill(fixture.baseUrl);
    const created = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/gateway/device-connections');
    await deviceDialog.getByRole('button', { name: '生成连接信息', exact: true }).click();
    const response = await created, body = await response.json();
    assert.equal(response.status(), 200);
    const token = body.data.connection_info.federation_token;
    assert(token && !tokens.includes(token));
    tokens.push(token);
    const info = page.getByRole('dialog', { name: '设备连接信息', exact: true });
    await expect(info.locator('pre')).toContainText(token);
    await info.getByRole('button', { name: '关闭', exact: true }).click();
    await expect(info).toBeHidden();
    await expect(control.locator('.gateway-device-list article').filter({ hasText: name })).toBeVisible();
    return body.data.connection.connection_id;
  };
  result.phase = 'device-create-and-scope';
  const firstId = await createDevice('管理测试设备一');
  await expect(control.getByLabel('入站访问范围', { exact: true })).toContainText(fixture.workspaceId);
  await expect(control.locator('.gateway-device-list article').filter({ hasText: firstId })).toContainText('已授权');
  result.firstDeviceId = firstId;

  result.phase = 'revoke-cancel-and-real-conflict';
  const firstRow = control.locator('.gateway-device-list article').filter({ hasText: firstId });
  await firstRow.getByRole('button', { name: '撤销', exact: true }).click();
  const confirm = page.getByRole('dialog', { name: '撤销设备访问权限', exact: true });
  await expect(confirm).toContainText('管理测试设备一');
  await confirm.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(result.writes.revoke, 0);
  // 在原确认打开时经真实 owner 撤销，随后确认得到真实404；不使用HTTP替身。
  await firstRow.getByRole('button', { name: '撤销', exact: true }).click();
  await expect(confirm).toBeVisible();
  await api('/api/gateway/device-connections/' + encodeURIComponent(firstId), 'DELETE');
  const failed = page.waitForResponse(response => response.request().method() === 'DELETE'
    && new URL(response.url()).pathname.endsWith(firstId) && response.status() === 404);
  await confirm.getByRole('button', { name: '撤销', exact: true }).click();
  const failure = await failed;
  await expect(control.getByRole('alert')).toContainText(failure.headers()['x-request-id']);
  await expect(control.locator('.gateway-device-list article').filter({ hasText: firstId })).toHaveCount(0);
  await control.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(control.getByRole('alert')).toHaveCount(0);

  result.phase = 'device-replace-and-revoke';
  const secondId = await createDevice('管理测试设备二');
  const secondRow = control.locator('.gateway-device-list article').filter({ hasText: secondId });
  await secondRow.getByRole('button', { name: '撤销', exact: true }).click();
  await expect(confirm).toContainText('管理测试设备二');
  await confirm.getByRole('button', { name: '撤销', exact: true }).click();
  await expect(control.getByText('暂无已授权的外部设备', { exact: true })).toBeVisible();
  const devices = await api('/api/gateway/device-connections');
  assert.deepEqual(devices.data.items, []);
  assert.deepEqual((await api('/api/gateway/workspaces')).data.items.map(item => item.workspace_id).sort(), originalWorkspaceIds);
  const unauthorized = await page.evaluate(async ({ token, baseUrl }) => {
    const response = await fetch(baseUrl + '/api/gateway/federation/manifest', { headers: { 'X-BoxTeam-Federation-Token': token } });
    return response.status;
  }, { token: tokens.at(-1), baseUrl: fixture.baseUrl });
  assert.equal(unauthorized, 401, '已撤销设备凭据必须失效');

  result.phase = 'representative-warm-api';
  // 此切片只迁移入口；选原设备列表作为唯一代表列表，未新增详情API。
  result.apiSamples = await page.evaluate(async () => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const samples = [];
    for (let index = 0; index < 21; index++) {
      const started = performance.now();
      const response = await fetch('/api/gateway/device-connections', { headers: { 'X-Local-Token': token } });
      const text = await response.text(), elapsedMs = performance.now() - started, body = JSON.parse(text);
      if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(text);
      samples.push({ elapsedMs, bytes: new TextEncoder().encode(text).length, requestId: body.request_id, count: body.data.items.length });
    }
    return samples;
  });
  const warm = result.apiSamples.slice(1).map(sample => sample.elapsedMs).sort((a, b) => a - b);
  result.apiBudget = { p50: warm[Math.ceil(warm.length * .5) - 1], p95: warm[Math.ceil(warm.length * .95) - 1], max: warm.at(-1) };
  assert(result.apiBudget.p95 <= 500 && result.apiBudget.max <= 1500, JSON.stringify(result.apiBudget));

  result.phase = 'f1-and-close-lifecycle';
  await closeControl();
  await command('workbench.action.showCommands');
  const quick = page.locator('.quick-input-widget');
  await quick.locator('input').first().fill('>连接与凭据');
  await quick.getByRole('option').filter({ hasText: '连接与凭据' }).click();
  await expect(control.getByLabel('入站访问范围', { exact: true })).toContainText(fixture.gatewayId);
  await expect(control.getByText('暂无已授权的外部设备', { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(artifacts, 'gateway-credentials.png') });
  await closeControl();
  await page.getByRole('button', { name: '帐户', exact: true }).click();
  await page.getByRole('menuitem', { name: '连接与凭据', exact: true }).click();
  await expect(control.getByLabel('入站访问范围', { exact: true })).toContainText(fixture.gatewayId);
  await expect(control.getByText('暂无已授权的外部设备', { exact: true })).toBeVisible();
  result.accountsEntry = true;
  await closeControl();
  await Promise.all([...pending]);
  const closedReads = result.requests.length;
  await page.waitForTimeout(600);
  assert.equal(result.requests.length, closedReads, '关闭控制中心后不能继续读设备或授权');
  await command('boxteam.flushViewState');
  result.secretAbsentFromStorage = !(await page.evaluate(async needles => {
    const leaks = value => needles.some(needle => String(value).includes(needle));
    if (Object.values(localStorage).some(leaks)) return true;
    for (const { name } of await indexedDB.databases()) {
      if (!name) continue;
      const database = await new Promise((resolve, reject) => { const request = indexedDB.open(name); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
      try {
        for (const storeName of database.objectStoreNames) {
          const values = await new Promise((resolve, reject) => { const request = database.transaction(storeName, 'readonly').objectStore(storeName).getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
          if (values.some(value => leaks(value instanceof ArrayBuffer ? new TextDecoder().decode(value) : ArrayBuffer.isView(value) ? new TextDecoder().decode(value) : JSON.stringify(value)))) return true;
        }
      } finally { database.close(); }
    }
    return false;
  }, tokens));
  assert(result.secretAbsentFromStorage);
  result.phase = 'completed';
} catch (error) {
  result.error = safe(error.stack ?? error);
  process.exitCode = 1;
  if (page) {
    // 失败时可能仍停留一次性凭据显示，截图前遮住秘密正文。
    try { await page.screenshot({ path: path.join(artifacts, 'failure.png'), mask: [page.locator('.gateway-device-info pre')] }); }
    catch (captureError) { cleanupErrors.push(safe(captureError)); }
  }
} finally {
  await Promise.allSettled([...pending]);
  try { await context?.close(); } catch (error) { cleanupErrors.push(safe(error)); }
  try { await browser?.close(); } catch (error) { cleanupErrors.push(safe(error)); }
  if (cleanupErrors.length) { result.cleanupErrors = cleanupErrors; process.exitCode = 1; }
  await writeFile(path.join(artifacts, 'result.json'), safe(JSON.stringify(result, null, 2)));
}
