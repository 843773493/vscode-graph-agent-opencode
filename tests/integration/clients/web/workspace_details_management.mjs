import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_WORKSPACE_DETAILS_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], unexpectedCalls: [], requests: [], route_stubs: [], boundary: fixture.transportBoundary };
let browser, context, page, failure;
const pending = new Set(), cleanupErrors = [];
const key = (gatewayId, workspaceId) => JSON.stringify([gatewayId, workspaceId]);
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  context = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
  page = await context.newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    if (/tool-approvals|\/messages(?:$|\?)|\/chat\/completions|\/responses(?:$|\?)/.test(new URL(request.url()).pathname)) result.unexpectedCalls.push(request.url());
  });
  page.on('response', response => {
    const request = response.request(), url = new URL(response.url());
    const mutation = request.method() !== 'GET' && (url.pathname === '/api/gateway/managed-workspaces'
      || /^\/api\/gateway\/workspaces\/[^/]+(?:\/activate)?$/.test(url.pathname));
    if (!mutation && !['/api/gateway/local-directories', '/api/gateway/diagnostics', '/api/v1/workspace'].includes(url.pathname)) return;
    const operation = (async () => {
      const payload = await response.json(), requestId = response.headers()['x-request-id'];
      assert(requestId);
      assert.equal(payload.request_id, requestId);
      result.requests.push({ phase: result.phase, path: url.pathname, query: url.search, method: request.method(), status: response.status(), requestId,
        targetGateway: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'],
        body: request.postData() ? request.postDataJSON() : undefined });
    })();
    pending.add(operation); void operation.finally(() => pending.delete(operation)).catch(error => cleanupErrors.push(error.stack));
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (url, { gatewayId = fixture.gatewayA, workspaceId = fixture.workspaceId, method = 'GET', body } = {}) => page.evaluate(async ({ url, gatewayId, workspaceId, method, body }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { method, headers: { 'X-Local-Token': credential.token, 'X-BoxTeam-Target-Gateway-Id': gatewayId,
      'X-BoxTeam-Workspace-Id': workspaceId, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
    const payload = await response.json(), requestId = response.headers.get('X-Request-ID');
    if (!response.ok || !requestId || payload.request_id && payload.request_id !== requestId) throw new Error(`${url}: ${response.status} ${JSON.stringify(payload)}`);
    return payload.data;
  }, { url, gatewayId, workspaceId, method, body });
  const details = page.getByRole('dialog', { name: '工作区详情', exact: true });
  const showDetails = async (gatewayId, workspaceId) => {
    await command('boxteam.workspace.details', { gatewayId, workspaceId });
    await expect(details).toBeVisible();
    await expect(details).toContainText(gatewayId);
    await expect(details).toContainText(workspaceId);
  };
  const closeDetails = () => details.getByRole('button', { name: '关闭工作区详情', exact: true }).first().click();
  const addDialog = page.getByRole('dialog', { name: '选择工作区所在设备', exact: true });
  const directories = page.getByRole('dialog', { name: '选择工作区文件夹', exact: true });
  const chooseDirectory = async (device, root) => {
    await page.getByRole('button', { name: '添加工作区', exact: true }).click();
    await addDialog.getByRole('button').filter({ hasText: device }).click();
    const address = directories.getByRole('textbox', { name: '目录地址', exact: true });
    await expect(address).toBeEnabled();
    const directoryPath = root + '/';
    await address.fill(directoryPath);
    const read = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/local-directories' && new URL(response.url()).searchParams.get('path') === directoryPath);
    await address.press('Enter');
    const response = await read;
    assert.equal(response.status(), 200, await response.text());
    const directory = (await response.json()).data;
    assert(directory.path.startsWith('boxteam://gateway/'), directory.path);
    assert(directory.path.includes(device === '当前电脑' ? fixture.gatewayA : fixture.gatewayB), directory.path);
    await expect(directories.getByRole('button', { name: '添加工作区', exact: true })).toBeEnabled();
    return directory.path;
  };
  const submitRegistration = async binding => {
    const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/managed-workspaces' && response.request().method() === 'POST', { timeout: 150000 });
    await directories.getByRole('button', { name: '添加工作区', exact: true }).click();
    const receipt = await response;
    assert.equal(receipt.request().postDataJSON().root_path, binding);
    assert.equal(receipt.request().postDataJSON().create_directory, false);
    return receipt;
  };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  const primaryRow = page.getByTestId('workspace-node-' + key(fixture.gatewayA, fixture.workspaceId));
  await expect(primaryRow).toBeVisible({ timeout: 30000 });
  result.initialization = { health: await api('/api/gateway/health'), workspaces: await api('/api/gateway/workspaces'), workspace: await api('/api/v1/workspace') };
  assert.equal(result.initialization.health.gateway_id, fixture.gatewayA);
  assert.equal(result.initialization.workspace.workspace_id, fixture.workspaceId);

  result.phase = 'remote-details-owner';
  const firstStarted = performance.now();
  await showDetails(fixture.gatewayB, fixture.workspaceId);
  result.firstAvailableMs = performance.now() - firstStarted;
  assert(result.firstAvailableMs <= 3000);
  const remote = result.initialization.workspaces.items.find(item => item.gateway_id === fixture.gatewayB);
  await expect(details).toContainText(fixture.gatewayA);
  await expect(details).toContainText(remote.backend_id ?? '未分配');
  await expect(details).toContainText(fixture.connectionId);
  await expect(details).toContainText(path.join(fixture.remoteRoot, '.boxteam'));
  result.remoteDataDirectory = path.join(fixture.remoteRoot, '.boxteam');
  await expect(details).toContainText('移除只注销登记');
  assert.deepEqual((await api('/api/gateway/workspaces')).active_workspace, { gateway_id: fixture.gatewayA, workspace_id: fixture.workspaceId });
  await details.getByRole('button', { name: '工作区设置', exact: true }).click();
  const quick = page.locator('.quick-input-widget');
  // 目标配置默认拒绝是 Gateway 的真实403；本次入口必须保留它的原request_id。
  await expect(details.getByRole('alert')).toContainText('configuration_read');
  const errorText = await details.getByRole('alert').innerText();
  assert(errorText.includes('request_id=') && errorText.includes(fixture.gatewayB) && errorText.includes(fixture.workspaceId));
  result.remoteSettingsFailure = errorText;
  await closeDetails();

  result.phase = 'remote-directory-permission';
  const remoteBinding = await chooseDirectory('详情目标 B', fixture.remoteRoot);
  const denied = await submitRegistration(remoteBinding);
  assert.equal(denied.status(), 403, await denied.text());
  await expect(directories.getByRole('alert')).toContainText(denied.headers()['x-request-id']);
  await expect(directories.getByRole('alert')).toContainText(fixture.connectionId);
  // 原公开失败合同保留状态与阶段，不复制远端正文；真实目标拒绝由 Python 的 HTTP 旁路记录交叉核对。
  await expect(directories.getByRole('alert')).toContainText('403 Forbidden');
  await expect(directories.getByRole('alert')).toContainText('stage=workspace_create, error=HTTPStatusError');
  result.deniedRegistration = { requestId: denied.headers()['x-request-id'], status: denied.status(),
    detail: (await denied.json()).detail, visibleError: await directories.getByRole('alert').innerText() };
  await directories.getByRole('button', { name: '取消', exact: true }).click();

  result.phase = 'local-registration-cancel';
  let binding = await chooseDirectory('当前电脑', fixture.workspaceRoot);
  await directories.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(result.requests.filter(item => item.method === 'POST' && item.path === '/api/gateway/managed-workspaces').length, 1);
  result.phase = 'local-registration';
  binding = await chooseDirectory('当前电脑', fixture.workspaceRoot);
  const created = await submitRegistration(binding);
  assert.equal(created.status(), 200, await created.text());
  await expect(directories).toBeHidden({ timeout: 30000 });
  const registered = (await api('/api/gateway/workspaces')).items.find(item => item.gateway_id === fixture.gatewayA && item.workspace_id !== fixture.workspaceId);
  assert(registered && registered.backend_id && registered.backend_generation);
  result.workspaceId = registered.workspace_id;
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await expect(details).toContainText(registered.backend_id);
  await expect(details).toContainText(registered.backend_generation);
  await expect(details.locator('li')).toContainText(registered.workspace_id);
  result.phase = 'rename-cancel-and-save';
  await details.getByRole('button', { name: '重命名', exact: true }).click();
  const rename = page.getByRole('dialog', { name: '重命名工作区', exact: true });
  await rename.getByRole('textbox', { name: '工作区名称', exact: true }).fill('取消工作区名称');
  await rename.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(result.requests.filter(item => item.method === 'PATCH').length, 0);
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await details.getByRole('button', { name: '重命名', exact: true }).click();
  await rename.getByRole('textbox', { name: '工作区名称', exact: true }).fill('详情注册工作区');
  const renamed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/workspaces/' + registered.workspace_id && response.request().method() === 'PATCH');
  await rename.getByRole('button', { name: '保存', exact: true }).click();
  assert.equal((await renamed).status(), 200);
  await expect(rename).toBeHidden();
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await expect(details).toContainText('详情注册工作区');
  await expect(details).toContainText(path.join(fixture.workspaceRoot, '.boxteam'));
  result.localDataDirectory = path.join(fixture.workspaceRoot, '.boxteam');

  result.phase = 'frozen-local-settings';
  await details.getByRole('button', { name: '工作区设置', exact: true }).click();
  await expect(quick).toContainText('workspace_root_mutable_override');
  await quick.locator('.monaco-list-row').filter({ hasText: 'workspace_root_mutable_override' }).click();
  await expect(details).toBeHidden();
  const header = page.locator('.settings-context-label:visible');
  await expect(header).toContainText(fixture.gatewayA);
  await expect(page.locator('.settings-category-context:visible select[aria-label="配置范围"] option:checked')).toHaveText('工作区');
  await header.locator('summary').click();
  await expect(header.getByRole('combobox', { name: '配置来源', exact: true }).locator('option:checked')).toHaveText('工作区设置');
  result.phase = 'frozen-workspace-logs';
  await showDetails(fixture.gatewayA, registered.workspace_id);
  const diagnosticsResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/diagnostics' && new URL(response.url()).searchParams.get('workspace_id') === registered.workspace_id);
  await details.getByRole('button', { name: '工作区日志', exact: true }).click();
  const diagnostics = await diagnosticsResponse;
  assert.equal(diagnostics.status(), 200, await diagnostics.text());
  assert.equal(diagnostics.request().headers()['x-boxteam-target-gateway-id'], fixture.gatewayA);
  const diagnosticPanel = page.locator('.settings-editor:visible');
  await expect(diagnosticPanel.locator('.settings-category-context select[aria-label="配置工作区"] option:checked')).toHaveText(registered.name);
  await command('workbench.action.closeActiveEditor');

  result.phase = 'open-original-workspace-action';
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await details.getByRole('button', { name: '打开工作区', exact: true }).click();
  await expect(details).toBeHidden();
  assert.deepEqual((await api('/api/gateway/workspaces')).active_workspace, { gateway_id: fixture.gatewayA, workspace_id: registered.workspace_id });
  await command('boxteam.workspace.activate', { gatewayId: fixture.gatewayA, workspaceId: fixture.workspaceId });
  await command('workbench.action.closeActiveEditor');
  const row = page.getByTestId('workspace-node-' + key(fixture.gatewayA, registered.workspace_id));
  await row.getByRole('button', { name: '折叠工作区 详情注册工作区', exact: true }).click();
  const session = await api('/api/v1/sessions', { workspaceId: registered.workspace_id, method: 'POST', body: { title: '注销保留的会话' } });
  result.sessionId = session.session_id;
  result.phase = 'remove-registration';
  const confirm = page.locator('form.workspace-action-dialog');
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await details.getByRole('button', { name: '移除注册', exact: true }).click();
  await expect(confirm).toContainText(registered.workspace_id);
  await confirm.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(result.requests.filter(item => item.method === 'DELETE').length, 0);
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await details.getByRole('button', { name: '移除注册', exact: true }).click();
  await expect(confirm).toContainText('会话文件不会被删除');
  const removed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/workspaces/' + registered.workspace_id && response.request().method() === 'DELETE', { timeout: 150000 });
  await confirm.getByRole('button', { name: '删除', exact: true }).click();
  const removal = await removed;
  assert.equal(removal.status(), 409, await removal.text());
  const blocked = (await removal.json()).detail;
  assert.equal(blocked.status, 'blocked');
  assert.equal(blocked.gateway_id, fixture.gatewayA);
  assert.equal(blocked.workspace_id, registered.workspace_id);
  assert.equal(blocked.backend_generation, registered.backend_generation);
  assert.deepEqual(blocked.affected_workspace_ids, [registered.workspace_id]);
  assert(blocked.blockers.some(item => item.kind === 'proxy_stream' && item.workspace_id === registered.workspace_id));
  await expect(confirm).toContainText('强制删除工作区');
  await confirm.getByRole('button', { name: '取消', exact: true }).click();
  result.observationReleaseLimitation = { status: removal.status(), requestId: removal.headers()['x-request-id'],
    blockers: blocked.blockers, release: '显式关闭 Settings 并折叠工作区仍持有目录观察流；取消强制操作后刷新释放' };
  assert.equal(result.requests.filter(item => item.method === 'DELETE').length, 1);
  assert((await api('/api/gateway/workspaces')).items.some(item => item.gateway_id === fixture.gatewayA && item.workspace_id === registered.workspace_id));

  result.phase = 'safe-remove-after-observation-release';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(primaryRow).toBeVisible({ timeout: 30000 });
  await expect(row).toBeVisible();
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await details.getByRole('button', { name: '移除注册', exact: true }).click();
  await expect(confirm).toContainText(registered.workspace_id);
  const safeRemoved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/workspaces/' + registered.workspace_id && response.request().method() === 'DELETE', { timeout: 150000 });
  await confirm.getByRole('button', { name: '删除', exact: true }).click();
  const safeRemoval = await safeRemoved;
  assert.equal(safeRemoval.status(), 200, await safeRemoval.text());
  assert.deepEqual(safeRemoval.request().postDataJSON().expected_workspace_ids, [registered.workspace_id]);
  assert.deepEqual(safeRemoval.request().postDataJSON().desired_workspace_ids, []);
  assert.equal(new URL(safeRemoval.url()).searchParams.get('force'), 'false');
  await expect(confirm).toBeHidden();
  await expect(row).toHaveCount(0);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(primaryRow).toBeVisible({ timeout: 30000 });
  await expect(row).toHaveCount(0);
  assert(!(await api('/api/gateway/workspaces')).items.some(item => item.gateway_id === fixture.gatewayA && item.workspace_id === registered.workspace_id));
  result.removedReload = true;

  result.phase = 'explicit-registration-keeps-session';
  binding = await chooseDirectory('当前电脑', fixture.workspaceRoot);
  const restored = await submitRegistration(binding);
  assert.equal(restored.status(), 200, await restored.text());
  await expect(directories).toBeHidden({ timeout: 30000 });
  assert.equal((await api('/api/v1/sessions/' + session.session_id, { workspaceId: registered.workspace_id })).session_id, session.session_id);
  result.sessionPreserved = true;
  assert.deepEqual((await api('/api/gateway/workspaces')).active_workspace, { gateway_id: fixture.gatewayA, workspace_id: fixture.workspaceId });
  await showDetails(fixture.gatewayA, registered.workspace_id);
  await page.screenshot({ path: path.join(artifacts, 'workspace-details.png') });
  await closeDetails();
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.unexpectedCalls, []);
  result.phase = 'completed';
} catch (error) { failure = error; result.error = error.stack ?? String(error); }
finally {
  await Promise.allSettled([...pending]);
  if (page && failure) { try { await page.screenshot({ path: path.join(artifacts, 'failure.png') }); } catch (error) { cleanupErrors.push(error.stack); } }
  try { await context?.close(); } catch (error) { cleanupErrors.push(error.stack); }
  try { await browser?.close(); } catch (error) { cleanupErrors.push(error.stack); }
  if (cleanupErrors.length) result.cleanupErrors = cleanupErrors;
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n');
  if (failure || cleanupErrors.length) throw new AggregateError([...(failure ? [failure] : []), ...cleanupErrors], '工作区详情验收失败');
}
