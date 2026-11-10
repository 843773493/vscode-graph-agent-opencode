import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { parse as parseJsonc } from 'jsonc-parser';

const input = JSON.parse(process.env.BOXTEAM_PLUGIN_UI_FIXTURE);
const result = { phase: 'initialization', pageErrors: [], externalRequests: [], configOwners: [], configWrites: [],
  privateInputWrites: [], installed: false, uninstalled: false };
const pluginRequests = [];
const managementReads = [];
let privateInputRequestCount = 0;
let sourceWriteRequestCount = 0;
let browser;
let page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(input.baseUrl).origin) result.externalRequests.push(request.url());
    if (url.pathname.startsWith('/api/gateway/service-plugins/')) pluginRequests.push(url.pathname);
    if ((request.method() === 'POST' && /\/service-plugins\/(inventory|status)$/.test(url.pathname))
      || (request.method() === 'GET' && /^\/api\/(gateway|v1)\/config\/sources(?:\/[^/]+)?$/.test(url.pathname))) {
      managementReads.push({ method: request.method(), path: url.pathname });
    }
  });
  page.on('response', async response => {
    const request = response.request();
    const url = new URL(response.url());
    if (request.method() === 'POST' && (url.pathname.endsWith('/config/plugin-private-inputs')
      || url.pathname.endsWith('/service-plugins/private-inputs'))) {
      const requestBody = JSON.parse(request.postData() ?? '{}');
      result.privateInputWrites.push({ path: url.pathname, status: response.status(), gatewayId: requestBody.gateway_id,
        workspaceId: requestBody.workspace_id ?? null, packageId: requestBody.package_id, pluginId: requestBody.plugin_id,
        packageVersion: requestBody.package_version, sourceKey: requestBody.source_key,
        parameterPointer: requestBody.parameter_pointer, returnedReference: response.ok() && !JSON.stringify(await response.json()).includes(requestBody.value) });
      return;
    }
    if (request.method() !== 'PUT' || !url.pathname.includes('/config/sources/')) return;
    const headers = await request.allHeaders();
    result.configWrites.push({ path: url.pathname, sourceKey: url.pathname.split('/').at(-1), status: response.status(),
      gatewayId: headers['x-boxteam-target-gateway-id'] ?? null, workspaceId: headers['x-boxteam-workspace-id'] ?? null,
      body: request.postData() });
  });
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && (url.pathname.endsWith('/config/plugin-private-inputs')
      || url.pathname.endsWith('/service-plugins/private-inputs'))) privateInputRequestCount++;
    if (request.method() === 'PUT' && url.pathname.includes('/config/sources/')) sourceWriteRequestCount++;
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const api = (path, options = {}) => page.evaluate(async ({ path, options }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...(options.headers ?? {}) },
      body: options.body && JSON.stringify(options.body) });
    const envelope = await response.json();
    if (!response.ok || envelope.code !== 0 || envelope.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(`${path}: ${response.status} ${JSON.stringify(envelope)}`);
    }
    return envelope.data;
  }, { path, options });
  result.initialization = [];
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    result.initialization.push({ path, data: await api(path) });
  }
  const active = result.initialization[1].data.active_workspace;
  assert(active?.workspace_id && active.gateway_id === result.initialization[0].data.gateway_id);
  const targets = result.initialization[1].data.items.filter(item => item.workspace_id === active.workspace_id && item.gateway_id === active.gateway_id);
  assert.equal(targets.length, 1);
  const workspaceName = targets[0].name;
  result.owner = { gatewayId: active.gateway_id, workspaceId: active.workspace_id };
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
  const view = page.locator('.service-plugin-packages-view');
  await expect(view).toBeVisible();
  const verifyScopeReads = async (scope, expected) => {
    const start = managementReads.length;
    const statusResponse = scope === 'workspace' ? page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/gateway/service-plugins/status') : undefined;
    await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
    await expect(view.locator('.service-plugin-package')).toBeVisible();
    const reads = managementReads.slice(start);
    assert.deepEqual(reads.map(item => item.path).sort(), expected.sort());
    (result.scopeReads ??= {})[scope] = reads;
    const summary = view.getByLabel('工作区插件配置交接');
    if (statusResponse) {
      const status = await (await statusResponse).json();
      const handoff = status.data.workspace_configurations.find(item => item.workspace_id === active.workspace_id);
      assert(handoff, '工作区启用后必须收到所属 owner 的配置交接状态');
      await expect(summary).toContainText(`工作区 ${active.workspace_id} 配置交接：${handoff.state}`);
      result.workspaceHandoff = handoff;
    } else {
      await expect(summary).toHaveCount(0);
    }
  };

  const quick = page.locator('.quick-input-widget');
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click();
  };
  const inputQuick = async value => {
    await expect(quick).toBeVisible();
    const field = quick.locator('input').first();
    await field.fill(value);
    await field.press('Enter');
  };
  const installButton = view.getByRole('button', { name: '安装插件包', exact: true });
  await installButton.click();
  await pick('ZIP 文件');
  await inputQuick(input.packagePath);
  await expect(view).toContainText('ui.preference-probe@1.0.0', { timeout: 30_000 });
  const search = view.getByRole('searchbox', { name: '搜索已安装插件包' });
  await search.fill('不存在的插件包');
  await expect(view.locator('.service-plugin-package')).toBeHidden();
  await search.fill('ui.preference-probe');
  await expect(view.locator('.service-plugin-package')).toBeVisible();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
  await expect(view.locator('.service-plugin-package')).toBeVisible();
  await expect(search).toHaveValue('ui.preference-probe');
  await search.clear();
  await verifyScopeReads('gateway', ['/api/gateway/service-plugins/inventory', '/api/gateway/service-plugins/status']);
  result.installed = true;
  result.phase = 'gateway-binding';

  const openPlugin = async name => {
    await view.getByRole('button', { name, exact: true }).click();
    await expect(view.locator('.service-plugin-details')).toBeVisible();
  };
  const enable = async (name, sourceKey, confirmLabel) => {
    await openPlugin(name);
    const toggle = view.locator('.service-plugin-details button').first();
    await expect(toggle).toHaveText('启用此范围');
    await toggle.click();
    await pick(sourceKey);
    await pick(confirmLabel);
    await expect(quick).not.toBeVisible();
    await expect(view.locator('.service-plugin-details button').first()).toHaveText('停用此范围');
  };
  const unbind = async (name, sourceKey, confirmLabel) => {
    await openPlugin(name);
    await view.locator('.service-plugin-details button').filter({ hasText: '解除此范围绑定' }).click();
    await pick(sourceKey);
    await pick(confirmLabel);
    await expect(quick).not.toBeVisible();
    await expect(view.getByRole('button', { name: '解除此范围绑定', exact: true })).toHaveCount(0);
  };
  await enable('Gateway 偏好 · gateway-preference', 'gateway_mutable_override', '确认保存 Gateway 插件绑定');
  result.configOwners.push('gateway');
  result.phase = 'referenced-uninstall';
  const referencedUninstall = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/uninstall');
  await view.getByRole('button', { name: '卸载整个包', exact: true }).click();
  await pick('卸载整包');
  const rejection = await referencedUninstall;
  const rejectionBody = await rejection.json();
  assert.equal(rejection.status(), 409);
  assert.equal(rejectionBody.request_id, rejection.headers()['x-request-id']);
  assert.match(JSON.stringify(rejectionBody), /插件包仍被引用，不能卸载/);
  assert.match(JSON.stringify(rejectionBody), /configuration:gateway-preference/);
  await expect(page.locator('.notifications-toasts')).toContainText('configuration:gateway-preference');
  await expect(view.getByRole('button', { name: '解除此范围绑定', exact: true })).toBeVisible();
  await expect(view.getByRole('button', { name: '卸载整个包', exact: true })).toBeEnabled();
  result.referencedUninstall = { status: rejection.status(), response: rejectionBody };
  result.phase = 'gateway-binding';
  const editParameters = async (sourceKey, confirmation, label) => {
    await view.getByRole('button', { name: '编辑参数 JSON', exact: true }).click();
    await pick(sourceKey);
    await pick('编辑此来源的参数');
    await inputQuick(JSON.stringify({ label }));
    const submitted = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname.endsWith(`/config/sources/${sourceKey}`));
    await pick(confirmation);
    const response = await submitted;
    assert.equal(response.status(), 200);
    const receipt = await response.json();
    assert.equal(receipt.data.edit.state, 'active');
    assert.equal(receipt.data.edit.source_saved, true);
    await expect(quick).not.toBeVisible();
    await expect(view.getByRole('button', { name: '编辑参数 JSON', exact: true })).toBeVisible();
  };
  const setSecret = async (name, sourceKey, value, scopeWorkspace) => {
    await openPlugin(name);
    await view.getByRole('button', { name: '管理秘密参数 /credential', exact: true }).click();
    await pick(sourceKey);
    await pick('设置秘密参数');
    await inputQuick(value);
    const privateWrite = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith(scopeWorkspace ? '/config/plugin-private-inputs' : '/service-plugins/private-inputs'));
    const configWrite = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname.endsWith(`/config/sources/${sourceKey}`));
    await pick('确认保存秘密');
    const privateResponse = await privateWrite;
    assert.equal(privateResponse.status(), 200);
    const response = await configWrite;
    assert.equal(response.status(), 200);
    const receipt = await response.json();
    assert.equal(receipt.data.edit.source_saved, true);
    await expect(quick).not.toBeVisible();
  };
  const openSecretInput = async name => {
    await openPlugin(name);
    await view.getByRole('button', { name: '管理秘密参数 /credential', exact: true }).click();
    await pick('gateway_mutable_override');
    await pick('设置秘密参数');
    const password = quick.locator('input[type="password"]');
    await expect(password).toHaveCount(1);
    return password;
  };
  const initialPrivateWrites = privateInputRequestCount;
  const initialSourceWrites = sourceWriteRequestCount;
  const cancelledPassword = await openSecretInput('Gateway 偏好 · gateway-preference');
  await cancelledPassword.fill('cancelled-password-sentinel');
  await cancelledPassword.press('Escape');
  await expect(quick).not.toBeVisible();
  assert.equal(privateInputRequestCount, initialPrivateWrites);
  assert.equal(sourceWriteRequestCount, initialSourceWrites);
  const cancelledConfirmationPassword = await openSecretInput('Gateway 偏好 · gateway-preference');
  await cancelledConfirmationPassword.fill('cancelled-confirmation-sentinel');
  await cancelledConfirmationPassword.press('Enter');
  await pick('取消');
  await expect(quick).not.toBeVisible();
  assert.equal(privateInputRequestCount, initialPrivateWrites);
  assert.equal(sourceWriteRequestCount, initialSourceWrites);
  await editParameters('gateway_mutable_override', '确认保存 Gateway 插件参数', 'Gateway 独立参数');
  await setSecret('Gateway 偏好 · gateway-preference', 'gateway_mutable_override',
    'gateway-secret-sentinel', false);
  await openPlugin('Gateway 偏好 · gateway-preference');
  await view.getByRole('button', { name: '管理秘密参数 /credential', exact: true }).click();
  await pick('gateway_mutable_override');
  await pick('清除此来源的秘密覆盖');
  const gatewaySecretClear = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/config/sources/gateway_mutable_override'));
  await pick('确认保存 Gateway 插件秘密参数');
  assert.equal((await gatewaySecretClear).status(), 200);

  await view.getByLabel('插件管理范围').selectOption('workspace');
  await pick(workspaceName);
  await expect(view).toContainText('工作区偏好 · workspace-preference');
  await enable('工作区偏好 · workspace-preference', 'workspace_root_mutable_override', '确认保存工作区插件绑定');
  await verifyScopeReads('workspace', ['/api/gateway/service-plugins/inventory', '/api/gateway/service-plugins/status',
    '/api/v1/config/sources', '/api/v1/config/sources/workspace_root_mutable_override']);
  result.configOwners.push('workspace');
  await editParameters('workspace_root_mutable_override', '确认保存工作区插件参数', '工作区独立参数');
  await setSecret('工作区偏好 · workspace-preference', 'workspace_root_mutable_override',
    'workspace-secret-sentinel', true);
  const gatewayDocument = await api('/api/gateway/config/sources/gateway_mutable_override');
  const workspaceDocument = await api(`/api/v1/workspaces/${active.workspace_id}/config/sources/workspace_root_mutable_override`,
    { headers: { 'X-BoxTeam-Workspace-Id': active.workspace_id } });
  assert.equal(gatewayDocument.effective.plugins.configuration.values['ui.preference-probe']['gateway-preference'].label, 'Gateway 独立参数');
  assert.equal(workspaceDocument.effective.plugins.configuration.values['ui.preference-probe']['workspace-preference'].label, '工作区独立参数');
  assert.equal(workspaceDocument.effective.plugins.configuration.values['ui.preference-probe']['workspace-preference'].credential.kind, 'plugin_private_file');
  assert.equal(gatewayDocument.effective.plugins.configuration.values['ui.preference-probe']['gateway-preference'].credential, undefined);
  assert.equal(result.privateInputWrites.length, 2);
  assert.ok(result.privateInputWrites.every(write => write.status === 200 && write.returnedReference
    && write.packageVersion === '1.0.0' && write.sourceKey === (write.workspaceId ? 'workspace_root_mutable_override' : 'gateway_mutable_override')
    && write.parameterPointer === '/credential'));
  assert.ok(result.configWrites.every(write => !write.body?.includes('gateway-secret-sentinel')
    && !write.body?.includes('workspace-secret-sentinel')));
  result.parametersEdited = true;

  await view.getByLabel('插件管理范围').selectOption('defaults');
  await expect(view).toContainText('工作区偏好 · workspace-preference');
  await verifyScopeReads('defaults', ['/api/gateway/service-plugins/inventory', '/api/gateway/config/sources',
    '/api/gateway/config/sources/gateway_mutable_override']);
  await enable('工作区偏好 · workspace-preference', 'gateway_mutable_override', '确认保存 Gateway 插件绑定');
  result.configOwners.push('defaults');
  assert.deepEqual(new Set(result.configOwners), new Set(['gateway', 'workspace', 'defaults']));

  result.phase = 'unbind-and-uninstall';
  await view.getByLabel('插件管理范围').selectOption('gateway');
  await expect(view).toContainText('Gateway 偏好 · gateway-preference');
  await unbind('Gateway 偏好 · gateway-preference', 'gateway_mutable_override', '确认保存 Gateway 插件绑定');
  await view.getByLabel('插件管理范围').selectOption('workspace');
  await pick(workspaceName);
  await unbind('工作区偏好 · workspace-preference', 'workspace_root_mutable_override', '确认保存工作区插件绑定');
  await view.getByLabel('插件管理范围').selectOption('defaults');
  await unbind('工作区偏好 · workspace-preference', 'gateway_mutable_override', '确认保存 Gateway 插件绑定');
  await view.getByLabel('插件管理范围').selectOption('gateway');
  result.phase = 'stopped-plugin-settings';
  await expect(view).toContainText('Gateway 偏好 · gateway-preference');
  const settingsRequestStart = pluginRequests.length;
  const openSettings = async () => {
    await page.evaluate(() => { void globalThis.workbench.commands.executeCommand('boxteam.settings.pluginParameters'); });
    await pick('Gateway 插件参数');
    await pick('Gateway 偏好');
  };
  await openSettings();
  await pick('查看参数 schema');
  const schemaDialog = page.locator('.monaco-dialog-box[role="dialog"]');
  await expect(schemaDialog).toContainText('ui.preference-probe/gateway-preference@1.0.0');
  await expect(schemaDialog).toContainText(active.gateway_id);
  await expect(schemaDialog).toContainText('writeOnly');
  await schemaDialog.getByRole('button', { name: 'OK', exact: true }).click();
  await openSettings();
  await pick('编辑参数 JSON');
  await pick('gateway_mutable_override');
  await pick('编辑此来源的参数');
  await inputQuick(JSON.stringify({ label: '设置入口独立参数' }));
  const settingsSave = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/config/sources/gateway_mutable_override'));
  await pick('确认保存 Gateway 插件参数');
  assert.equal((await settingsSave).status(), 200);
  await expect(quick).not.toBeVisible();
  assert.deepEqual(pluginRequests.slice(settingsRequestStart), [
    '/api/gateway/service-plugins/configuration-schemas', '/api/gateway/service-plugins/configuration-schemas',
  ]);
  const stoppedSettings = await api('/api/gateway/config/sources/gateway_mutable_override');
  assert.equal(parseJsonc(stoppedSettings.jsonc).plugins.configuration.values['ui.preference-probe']['gateway-preference'].label, '设置入口独立参数');
  assert(!stoppedSettings.effective.plugins.configuration);
  result.stoppedSettingsWithoutRuntimeRequests = true;
  result.phase = 'uninstall';
  const packageRow = view.locator('section').filter({ hasText: 'ui.preference-probe@1.0.0' }).first();
  await packageRow.getByRole('button', { name: '卸载整个包', exact: true }).click();
  await pick('卸载整包');
  await expect(view).toContainText('尚无插件包', { timeout: 30_000 });
  result.uninstalled = true;
  const residual = await api('/api/gateway/config/sources/gateway_mutable_override');
  assert.equal(parseJsonc(residual.jsonc).plugins.configuration.values['ui.preference-probe']['gateway-preference'].label, '设置入口独立参数');
  assert(!residual.effective.plugins.configuration);
  result.phase = 'mcp-search-entry';
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.search', '@mcp'));
  const mcpView = page.getByRole('button', { name: 'MCP Servers Section', exact: true });
  await expect(mcpView).toBeVisible();
  await expect(page.getByText('No MCP Servers found.', { exact: true }).first()).toBeVisible();
  await expect(view.getByLabel('插件管理范围')).toBeHidden();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.installedExtensions'));
  await expect(view.getByLabel('插件管理范围')).toBeVisible();
  result.mcpSearchAvailable = true;
  result.phase = 'complete';
  assert(result.configWrites.some(item => item.path === '/api/gateway/config/sources/gateway_mutable_override'
    && item.body?.includes('workspace_defaults')));
  assert(result.configWrites.some(item => item.path === '/api/gateway/config/sources/gateway_mutable_override'
    && item.body?.includes('gateway-preference')));
  assert(result.configWrites.some(item => item.path === '/api/v1/config/sources/workspace_root_mutable_override'
    && item.workspaceId === active.workspace_id && item.body?.includes('workspace-preference')));
  for (const write of result.configWrites) {
    const intent = JSON.parse(write.body);
    const source = parseJsonc(intent.jsonc);
    if (write.workspaceId) assert(source.plugins.runtime.entries.every(binding => !Object.hasOwn(binding, 'workspace_id')));
  }
  assert.deepEqual(result.externalRequests, []);
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
} catch (error) {
  result.failure = error instanceof Error ? error.stack : String(error);
  if (page) {
    result.startupError = await page.locator('#startup-error').textContent().catch(() => null);
    try { await page.screenshot({ path: `${input.artifacts}/plugin-management-ui-failure.png` }); }
    catch (diagnosticError) { result.screenshotError = String(diagnosticError); }
  }
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
  throw error;
} finally {
  await browser?.close();
}
