import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { parse as parseJsonc } from 'jsonc-parser';

const input = JSON.parse(process.env.BOXTEAM_PLUGIN_UI_FIXTURE);
const result = { phase: 'initialization', pageErrors: [], externalRequests: [], configOwners: [], configWrites: [],
  privateInputWrites: [], installed: false, uninstalled: false, exitedPids: [], runtimeVersions: [] };
result.activationTimings = [];
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
  const pluginStatusStreamTerminations = [];
  for (const event of ['requestfailed', 'requestfinished']) {
    page.on(event, request => {
      if (new URL(request.url()).pathname === '/api/gateway/service-plugins/status/events') {
        pluginStatusStreamTerminations.push({ event, request });
      }
    });
  }
  const api = (path, options = {}, raw = false, expectedStatus = 200) => page.evaluate(async ({ path, options, raw, expectedStatus }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...(options.headers ?? {}) },
      body: options.body && JSON.stringify(options.body) });
    const envelope = await response.json();
    if (response.status !== expectedStatus || (response.ok && !raw && envelope.code !== 0)
      || envelope.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(`${path}: ${response.status} ${JSON.stringify(envelope)}`);
    }
    return raw ? envelope : envelope.data;
  }, { path, options, raw, expectedStatus });
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
    const summary = view.locator('.service-plugin-packages-content > [data-plugin-workspace-handoff]');
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
  await expect(quick).toContainText(`插件安装 · 原生 Gateway ${active.gateway_id}`);
  await pick('ZIP 文件');
  await inputQuick(input.packagePath);
  await expect(view).toContainText('ui.preference-probe@1.0.0', { timeout: 30_000 });
  await expect(page.locator('.notifications-toasts')).toContainText(
    `已在 原生 Gateway ${active.gateway_id} 安装 ui.preference-probe@1.0.0`);
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

  const packageRowFor = version => view.locator('.service-plugin-package').filter({
    has: page.getByRole('heading', { name: `UI 配置验证包 · ui.preference-probe@${version}`, exact: true }),
  });
  const openPlugin = async name => {
    await packageRowFor('1.0.0').getByRole('button', { name, exact: true }).click();
    await expect(view.locator('.service-plugin-details')).toBeVisible();
  };
  const enable = async (name, sourceKey, confirmLabel, scope = 'bound') => {
    await openPlugin(name);
    const enableLabel = scope === 'defaults' ? '设为新建时启用' : '启用此范围';
    const disableLabel = scope === 'defaults' ? '设为新建时停用' : '停用此范围';
    const toggle = view.getByRole('button', { name: enableLabel, exact: true });
    await expect(toggle).toHaveText(enableLabel);
    const activationStarted = performance.now();
    await toggle.click();
    await pick(sourceKey);
    await pick(confirmLabel);
    await expect(quick).not.toBeVisible();
    await expect(view.getByRole('button', { name: disableLabel, exact: true })).toBeVisible();
    if (scope !== 'defaults') {
      const pluginId = name.split(' · ').at(-1);
      let slot = await runtimeSlot(pluginId, sourceKey === 'workspace_root_mutable_override' ? active.workspace_id : undefined);
      if (!slot.declarative) {
        // 配置保存不等于异步工作区交接已就绪，沿原15秒交互期限观察真实状态。
        await expect.poll(async () => {
          slot = await runtimeSlot(pluginId, sourceKey === 'workspace_root_mutable_override' ? active.workspace_id : undefined);
          return slot.state;
        }, { timeout: 15_000 }).toBe('ready');
        result.activationTimings.push({ pluginId, scope: slot.binding.workspace_id ? 'workspace' : 'gateway',
          gatewayId: active.gateway_id, workspaceId: slot.binding.workspace_id ?? null,
          packageVersion: slot.binding.package_version, instanceId: slot.instance.instance_id,
          state: slot.state, milliseconds: performance.now() - activationStarted,
          boundary: '真实开关点击起，含自动操作确认／来源预检／CAS／就绪及浏览器完整状态读取；单次样本不外推启动p95' });
      }
    }
  };
  const unbind = async (name, sourceKey, confirmLabel, workspaceId = active.workspace_id, scope = 'bound') => {
    const before = await runtimeSlot(name.includes('gateway-preference') ? 'gateway-preference' : 'workspace-preference',
      sourceKey === 'workspace_root_mutable_override' ? workspaceId : undefined);
    if (before?.instance) result.exitedPids.push(before.instance.process_id);
    await openPlugin(name);
    const unbindLabel = scope === 'defaults' ? '从新建默认值移除' : '解除此范围绑定';
    await view.getByRole('button', { name: unbindLabel, exact: true }).click();
    await pick(sourceKey);
    await pick(confirmLabel);
    await expect(quick).not.toBeVisible();
    await expect(view.getByRole('button', { name: unbindLabel, exact: true })).toHaveCount(0);
  };

  const runtimeSlot = async (pluginId, workspaceId) => {
    const snapshot = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: active.gateway_id } });
    return snapshot.plugins.find(item => item.binding.package_id === 'ui.preference-probe' && item.binding.plugin_id === pluginId
      && (item.binding.workspace_id ?? null) === (workspaceId ?? null));
  };
  const skillSourcePath = `/api/gateway/skill-sources/${active.gateway_id}/workspaces/${active.workspace_id}`;
  const guideEntries = async () => (await api(skillSourcePath)).entries.filter(entry =>
    entry.plugin?.package_id === 'ui.preference-probe' && entry.plugin.plugin_id === 'workspace-guide');
  const readGuide = async version => {
    await expect.poll(async () => (await guideEntries()).map(entry => entry.plugin.package_version)).toEqual([version]);
    const entries = await guideEntries();
    assert.equal(entries.length, 1);
    const entry = entries[0];
    assert.equal(entry.plugin.workspace_id, active.workspace_id);
    assert.equal(entry.plugin.package_version, version);
    const content = await api(`${skillSourcePath}/navigation`, { method: 'POST',
      body: { uri: entry.display_uri, operation: 'read' } });
    assert(content.content.includes(`UI_SKILL_${version}`));
    const slot = await runtimeSlot('workspace-guide', active.workspace_id);
    assert.equal(slot.declarative, true);
    assert.equal(slot.binding.enabled, true);
    assert.equal(slot.binding.package_version, version);
    assert.equal(slot.instance, null);
    assert.equal(slot.log_id, null);
    (result.declarativeSkillVersions ??= []).push(version);
    return entry;
  };
  assert.deepEqual(await guideEntries(), []);
  assert.equal(await runtimeSlot('workspace-guide', active.workspace_id), undefined);
  const callRuntime = async slot => {
    assert.equal(slot.gateway_id, active.gateway_id); assert.equal(slot.state, 'ready'); assert(slot.instance);
    const query = new URLSearchParams({ package_version: slot.binding.package_version,
      ...(slot.binding.workspace_id ? { workspace_id: slot.binding.workspace_id } : {}) });
    const value = await api(`/api/gateway/service-plugins/${slot.binding.package_id}/${slot.binding.plugin_id}/instances/${slot.instance.instance_id}/services/echo/value?${query}`, {}, true);
    assert.equal(value.instance_id, slot.instance.instance_id); assert.equal(value.version, slot.binding.package_version);
    assert.equal(value.pid, slot.instance.process_id);
    result.exitedPids.push(value.pid);
    return value;
  };
  await enable('Gateway 偏好 · gateway-preference', 'gateway_mutable_override', '确认保存 Gateway 插件绑定');
  const firstGateway = await runtimeSlot('gateway-preference'); await callRuntime(firstGateway);
  result.configOwners.push('gateway');
  result.phase = 'referenced-uninstall';
  const referencedUninstall = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/uninstall');
  await view.getByRole('button', { name: '卸载整个包', exact: true }).click();
  await expect(quick).toContainText(active.gateway_id);
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

  result.phase = 'explicit-version-switch';
  const gatewaySource = () => api('/api/gateway/config/sources/gateway_mutable_override');
  const configuredGateway = document => document.source_effective.plugins.runtime.entries.find(item =>
    item.package_id === 'ui.preference-probe' && item.plugin_id === 'gateway-preference');
  const beforeInstall = await gatewaySource();
  const beforeInstallRuntime = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: active.gateway_id } });
  result.incompatibleInstalls = [];
  for (const candidate of input.incompatiblePackages) {
    result.phase = `reject-incompatible-${candidate.runtime}`;
    const installing = page.waitForRequest(request => request.method() === 'POST'
      && new URL(request.url()).pathname.endsWith('/service-plugins/install'));
    await installButton.click(); await pick('ZIP 文件'); await inputQuick(candidate.path);
    const installingRequest = await installing;
    const request = installingRequest.postDataJSON();
    assert.equal(request.gateway_id, active.gateway_id);
    const accepted = await installingRequest.response();
    assert.equal(accepted?.status(), 200);
    await expect.poll(async () => (await api('/api/gateway/service-plugins/install-operation',
      { method: 'POST', body: { gateway_id: active.gateway_id, operation_id: request.operation_id } })).status).toBe('failed');
    const operation = await api('/api/gateway/service-plugins/install-operation',
      { method: 'POST', body: { gateway_id: active.gateway_id, operation_id: request.operation_id } });
    assert.equal(operation.artifact.package_id, 'ui.preference-probe');
    assert.equal(operation.artifact.package_version, candidate.version);
    assert.equal(operation.gateway_id, active.gateway_id);
    const failure = page.locator('.notifications-toasts .notification-toast')
      .filter({ hasText: `${candidate.runtime} 运行时不匹配` }).last();
    await expect(failure).toContainText(`ui.preference-probe@${candidate.version}`);
    await expect(failure).toContainText(active.gateway_id);
    await expect(failure).toContainText(`package=${candidate.required}`);
    await expect(failure).toContainText(`gateway=${candidate.actual}`);
    await expect(failure).toContainText('完整插件 ZIP');
    assert.equal((await gatewaySource()).jsonc, beforeInstall.jsonc);
    assert.deepEqual(await api('/api/gateway/service-plugins/status',
      { method: 'POST', body: { gateway_id: active.gateway_id } }), beforeInstallRuntime);
    await callRuntime(beforeInstallRuntime.plugins.find(item => item.binding.plugin_id === 'gateway-preference'));
    await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
    await expect(view.locator('.service-plugin-package')).toHaveCount(1);
    result.incompatibleInstalls.push({ ...operation, bindingKept: true });
  }
  result.phase = 'explicit-version-switch';
  for (const [index, path] of input.versionPaths.entries()) {
    await installButton.click();
    if (index === 0) {
      await pick('自有来源'); await pick('proof'); await inputQuick('ui.preference-probe');
      const installing = page.waitForRequest(request => request.method() === 'POST'
        && new URL(request.url()).pathname.endsWith('/service-plugins/install'));
      await pick('ui.preference-probe@1.1.0');
      const request = (await installing).postDataJSON();
      assert.equal(request.gateway_id, active.gateway_id);
      assert.equal(request.source_id, 'proof'); assert.equal(request.catalog_revision, 'catalog-v1');
      assert.deepEqual(request.artifact, input.sourceArtifact);
      await expect(packageRowFor('1.1.0')).toBeVisible();
      result.sourceUpdate = await api('/api/gateway/service-plugins/install-operation',
        { method: 'POST', body: { gateway_id: active.gateway_id, operation_id: request.operation_id } });
      assert.equal(result.sourceUpdate.status, 'succeeded');
      assert.equal(result.sourceUpdate.artifact.digest, input.sourceArtifact.digest);
    } else { await pick('ZIP 文件'); await inputQuick(path); }
    await expect(view.locator('.service-plugin-package')).toHaveCount(index + 2);
  }
  assert.equal((await gatewaySource()).jsonc, beforeInstall.jsonc);
  assert.deepEqual(await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: active.gateway_id } }), beforeInstallRuntime);
  result.installKeptBindings = true;
  const openVersion = async version => {
    await packageRowFor(version).getByRole('button', { name: 'Gateway 偏好 · gateway-preference', exact: true }).click();
    await expect(view.locator('.service-plugin-details')).toBeVisible();
  };
  const switchVersion = async (version, enabled) => {
    await openVersion(version);
    await expect(view.getByRole('button', { name: '编辑参数 JSON', exact: true })).toHaveCount(0);
    await view.getByRole('button', { name: '切换到此版本（保留开关）', exact: true }).click();
    await pick('gateway_mutable_override');
    await expect(quick).toContainText(`切换至 ${version}，保留原开关`);
    const saved = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname.endsWith('/config/sources/gateway_mutable_override'));
    await pick('确认保存 Gateway 插件绑定');
    assert.equal((await saved).status(), 200);
    await expect(view.getByLabel('插件配置与运行版本')).toContainText(`配置：${enabled ? '启用' : '停用'} @${version}`);
    const slot = await runtimeSlot('gateway-preference');
    if (enabled) { await callRuntime(slot); result.runtimeVersions.push(slot.instance.package_version); }
    else { assert.equal(slot.state, 'stopped'); assert.equal(slot.instance, null); }
    assert.deepEqual(configuredGateway(await gatewaySource()), { package_id: 'ui.preference-probe', plugin_id: 'gateway-preference',
      package_version: version, enabled });
  };
  await switchVersion('1.1.0', true);
  await switchVersion('1.0.0', true);
  const beforeInvalid = await gatewaySource(), writesBeforeInvalid = sourceWriteRequestCount;
  await openVersion('2.0.0');
  await view.getByRole('button', { name: '切换到此版本（保留开关）', exact: true }).click();
  await pick('gateway_mutable_override');
  await expect(page.locator('.notifications-toasts')).toContainText('未提交');
  await expect(quick).not.toBeVisible();
  assert.equal(sourceWriteRequestCount, writesBeforeInvalid);
  assert.equal((await gatewaySource()).jsonc, beforeInvalid.jsonc);
  assert.equal((await runtimeSlot('gateway-preference')).binding.package_version, '1.0.0');
  result.invalidVersionKeptBinding = true;
  result.phase = 'failed-version-and-explicit-retry';
  const beforeFailedSwitch = await runtimeSlot('gateway-preference');
  await openVersion('3.0.0');
  await view.getByRole('button', { name: '切换到此版本（保留开关）', exact: true }).click();
  await pick('gateway_mutable_override');
  const failedVersionSave = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/config/sources/gateway_mutable_override'));
  await pick('确认保存 Gateway 插件绑定');
  assert.equal((await failedVersionSave).status(), 200);
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('配置：启用 @3.0.0');
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('实际：failed');
  await expect(view.getByLabel('插件运行失败原因')).toContainText('启动实例失败');
  await expect(view.getByLabel('插件运行失败原因')).toContainText('ready 身份');
  result.failedVersion = await runtimeSlot('gateway-preference');
  assert.equal(result.failedVersion.binding.package_version, '3.0.0');
  assert.equal(result.failedVersion.binding.enabled, true);
  assert.equal(result.failedVersion.state, 'failed');
  assert.equal(result.failedVersion.instance, null);
  const oldBinding = beforeFailedSwitch.binding, oldInstance = beforeFailedSwitch.instance;
  await api(`/api/gateway/service-plugins/${oldBinding.package_id}/${oldBinding.plugin_id}/instances/${oldInstance.instance_id}/services/echo/value?package_version=${oldBinding.package_version}`,
    {}, true, 409);
  result.exitedPids.push(oldInstance.process_id);
  await view.getByRole('button', { name: '查看实例日志', exact: true }).click();
  await expect(view.getByLabel('插件实例日志')).toContainText('Uvicorn running');
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
  await expect(view.getByLabel('插件运行失败原因')).toContainText('启动实例失败');
  const afterFailedReads = await runtimeSlot('gateway-preference');
  assert.deepEqual(afterFailedReads, result.failedVersion, '刷新与状态读取不能回滚旧版或自动重试');
  const explicitlyRetried = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/service-plugins/restart'));
  const statusStreamAfterRetry = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/status/events');
  await view.getByRole('button', { name: '重启实例', exact: true }).click();
  assert.equal((await explicitlyRetried).status(), 200);
  const retryStreamResponse = await statusStreamAfterRetry;
  assert.equal(retryStreamResponse.status(), 200);
  assert.match(retryStreamResponse.headers()['content-type'] ?? '', /^text\/event-stream/);
  result.explicitRetry = await runtimeSlot('gateway-preference');
  assert.equal(result.explicitRetry.binding.package_version, '3.0.0');
  await callRuntime(result.explicitRetry);
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('实际：ready @3.0.0');
  await expect(view.getByLabel('插件运行失败原因')).toBeHidden();
  // 旧 `/status` 快照保持 ready；真实配置停用后，打开的视图必须由 SSE 显示 stopped。
  const staleStatusEnvelope = await api('/api/gateway/service-plugins/status',
    { method: 'POST', body: { gateway_id: active.gateway_id } }, true);
  const staleStatusSlot = staleStatusEnvelope.data.plugins.find(item => item.binding?.plugin_id === 'gateway-preference');
  assert.equal(staleStatusSlot?.state, 'ready');
  assert.equal(staleStatusSlot?.binding?.package_version, '3.0.0');
  let staleStatusResponses = 0;
  const runtimeStatusSnapshotUrl = url => url.pathname === '/api/gateway/service-plugins/status';
  await page.route(runtimeStatusSnapshotUrl, async route => {
    const requestId = `plugin-ui-stale-status-${++staleStatusResponses}`;
    await route.fulfill({ status: 200, contentType: 'application/json', headers: { 'X-Request-ID': requestId },
      body: JSON.stringify({ ...staleStatusEnvelope, request_id: requestId }) });
  });
  const setGatewayPluginEnabled = async enabled => {
    const document = await gatewaySource();
    const source = parseJsonc(document.jsonc);
    const binding = source.plugins.runtime.entries.find(item =>
      item.package_id === 'ui.preference-probe' && item.plugin_id === 'gateway-preference');
    assert(binding);
    binding.enabled = enabled;
    return api('/api/gateway/config/sources/gateway_mutable_override', { method: 'PUT', body: {
      gateway_id: active.gateway_id, source_key: 'gateway_mutable_override', jsonc: JSON.stringify(source),
      base_file_digest: document.file_digest, base_layer_revision: document.layer_revision,
      base_layer_digest: document.layer_digest, expected_active_revision: document.active_revision,
      expected_active_digest: document.active_digest,
      idempotency_key: `plugin-ui-status-sse-${enabled ? 'enable' : 'disable'}-${Date.now()}`,
    } });
  };
  const disabledByConfig = await setGatewayPluginEnabled(false);
  assert.equal(disabledByConfig.edit.state, 'active');
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('实际：stopped');
  const staleStatusRead = await runtimeSlot('gateway-preference');
  assert.equal(staleStatusRead.state, 'ready', '固定的 /status 旧快照仍应是 ready');
  assert.equal(staleStatusResponses, 1);
  await expect(view.getByLabel('插件配置与运行版本'),
    '页面运行状态必须来自已到达的 SSE 快照，而不是本次 /status 旧快照').toContainText('实际：stopped');
  await page.unroute(runtimeStatusSnapshotUrl);
  const reenabledByConfig = await setGatewayPluginEnabled(true);
  assert.equal(reenabledByConfig.edit.state, 'active');
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('实际：ready @3.0.0');
  result.runtimeStatusSseUi = { path: '/api/gateway/service-plugins/status/events', eventContentType:
    retryStreamResponse.headers()['content-type'], staleStatus: staleStatusSlot.state,
    stalePollState: staleStatusRead.state, renderedState: 'stopped', staleResponses: staleStatusResponses,
    configurationOwner: 'gateway_mutable_override' };
  await switchVersion('1.0.0', true);
  await openVersion('1.0.0');
  await view.getByRole('button', { name: '停用此范围', exact: true }).click();
  await pick('gateway_mutable_override'); await pick('确认保存 Gateway 插件绑定');
  await expect(view.getByRole('button', { name: '启用此范围', exact: true })).toBeVisible();
  result.exitedPids.push(beforeInstallRuntime.plugins.find(item => item.binding.plugin_id === 'gateway-preference').instance.process_id);
  const disabledUninstall = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/service-plugins/uninstall'));
  await packageRowFor('1.0.0').getByRole('button', { name: '卸载整个包', exact: true }).click(); await pick('卸载整包');
  assert.equal((await disabledUninstall).status(), 409);
  assert.equal((await runtimeSlot('gateway-preference')).binding.enabled, false);
  result.closedBindingBlockedUninstall = true;
  await switchVersion('1.1.0', false);
  await switchVersion('1.0.0', false);
  result.disabledVersionStayedDisabled = true;

  result.phase = 'declarative-skill-lifecycle';
  await view.getByLabel('插件管理范围').selectOption('workspace'); await pick(workspaceName);
  await enable('工作区指南 · workspace-guide', 'workspace_root_mutable_override', '确认保存工作区插件绑定');
  await expect(view.getByRole('button', { name: '重启实例', exact: true })).toHaveCount(0);
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('声明式，无运行实例');
  const firstGuide = await readGuide('1.0.0');
  await packageRowFor('1.1.0').getByRole('button', { name: '工作区指南 · workspace-guide', exact: true }).click();
  await view.getByRole('button', { name: '切换到此版本（保留开关）', exact: true }).click();
  await pick('workspace_root_mutable_override'); await pick('确认保存工作区插件绑定');
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('配置：启用 @1.1.0');
  const updatedGuide = await readGuide('1.1.0');
  assert.notEqual(firstGuide.display_uri, updatedGuide.display_uri);
  await api(`${skillSourcePath}/navigation`, { method: 'POST',
    body: { uri: firstGuide.display_uri, operation: 'read' } }, true, 404);
  await view.getByRole('button', { name: '停用此范围', exact: true }).click();
  await pick('workspace_root_mutable_override'); await pick('确认保存工作区插件绑定');
  await expect(view.getByRole('button', { name: '启用此范围', exact: true })).toBeVisible();
  await expect.poll(guideEntries).toEqual([]);
  const stoppedGuide = await runtimeSlot('workspace-guide', active.workspace_id);
  assert.equal(stoppedGuide.binding.enabled, false);
  assert.equal(stoppedGuide.instance, null);
  await api(`${skillSourcePath}/navigation`, { method: 'POST',
    body: { uri: updatedGuide.display_uri, operation: 'read' } }, true, 404);
  const guideUninstall = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/service-plugins/uninstall'));
  await packageRowFor('1.1.0').getByRole('button', { name: '卸载整个包', exact: true }).click(); await pick('卸载整包');
  const guideConflict = await guideUninstall;
  assert.equal(guideConflict.status(), 409);
  const guideConflictBody = await guideConflict.json();
  assert.equal(guideConflictBody.request_id, guideConflict.headers()['x-request-id']);
  assert.match(JSON.stringify(guideConflictBody), new RegExp(`workspace-configuration:${active.workspace_id}:workspace-guide`));
  await view.getByRole('button', { name: '解除此范围绑定', exact: true }).click();
  await pick('workspace_root_mutable_override'); await pick('确认保存工作区插件绑定');
  await expect(view.getByRole('button', { name: '解除此范围绑定', exact: true })).toHaveCount(0);
  await expect.poll(() => runtimeSlot('workspace-guide', active.workspace_id)).toBeUndefined();
  assert.equal((await runtimeSlot('gateway-preference')).binding.enabled, false);
  result.declarativeSkillLoop = true;
  await view.getByLabel('插件管理范围').selectOption('gateway');
  result.phase = 'explicit-version-switch';
  for (const version of ['1.1.0', '2.0.0', '3.0.0']) {
    await packageRowFor(version).getByRole('button', { name: '卸载整个包', exact: true }).click();
    await pick('卸载整包');
    await expect(packageRowFor(version)).toHaveCount(0);
  }
  await enable('Gateway 偏好 · gateway-preference', 'gateway_mutable_override', '确认保存 Gateway 插件绑定');
  result.explicitVersionSwitch = true;

  await view.getByLabel('插件管理范围').selectOption('workspace');
  await pick(workspaceName);
  await expect(view).toContainText('工作区偏好 · workspace-preference');
  await enable('工作区偏好 · workspace-preference', 'workspace_root_mutable_override', '确认保存工作区插件绑定');
  await verifyScopeReads('workspace', ['/api/gateway/service-plugins/inventory', '/api/gateway/service-plugins/status',
    '/api/v1/config/sources', '/api/v1/config/sources/workspace_root_mutable_override']);

  await expect.poll(async () => (await runtimeSlot('workspace-preference', active.workspace_id))?.state).toBe('ready');
  const workspaceReady = await runtimeSlot('workspace-preference', active.workspace_id); await callRuntime(workspaceReady);
  let gatewayReady = await runtimeSlot('gateway-preference'); await callRuntime(gatewayReady);
  assert.notEqual(gatewayReady.instance.process_id, workspaceReady.instance.process_id);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
  assert.equal((await runtimeSlot('gateway-preference')).instance.instance_id, gatewayReady.instance.instance_id);
  assert.equal((await runtimeSlot('workspace-preference', active.workspace_id)).instance.instance_id, workspaceReady.instance.instance_id);
  await view.getByLabel('插件管理范围').selectOption('gateway');
  await openPlugin('Gateway 偏好 · gateway-preference');
  const gatewayRestarted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/service-plugins/restart'));
  await view.getByRole('button', { name: '重启实例', exact: true }).click();
  const gatewayRestartResponse = await gatewayRestarted;
  assert.equal(gatewayRestartResponse.status(), 200);
  const gatewayRestart = await runtimeSlot('gateway-preference');
  assert.notEqual(gatewayRestart.instance.instance_id, gatewayReady.instance.instance_id);
  await callRuntime(gatewayRestart);
  assert.equal((await runtimeSlot('workspace-preference', active.workspace_id)).instance.instance_id, workspaceReady.instance.instance_id);
  result.exitedPids.push(gatewayReady.instance.process_id);
  result.gatewayRestartedIndependently = true;
  gatewayReady = gatewayRestart;
  await view.getByLabel('插件管理范围').selectOption('workspace'); await pick(workspaceName);
  await openPlugin('工作区偏好 · workspace-preference');
  const restarted = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/service-plugins/restart'));
  await view.getByRole('button', { name: '重启实例', exact: true }).click(); assert.equal((await restarted).status(), 200);
  const workspaceRestart = await runtimeSlot('workspace-preference', active.workspace_id);
  assert.notEqual(workspaceRestart.instance.instance_id, workspaceReady.instance.instance_id); await callRuntime(workspaceRestart);
  assert.equal((await runtimeSlot('gateway-preference')).instance.instance_id, gatewayReady.instance.instance_id);
  result.exitedPids.push(workspaceReady.instance.process_id);
  result.reloadKeptInstances = true;
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
  const existingBeforeDefaults = await runtimeSlot('workspace-preference', active.workspace_id);
  const gatewayBeforeDefaults = await runtimeSlot('gateway-preference');
  await enable('工作区偏好 · workspace-preference', 'gateway_mutable_override', '确认保存 Gateway 新建工作区默认模板', 'defaults');
  result.configOwners.push('defaults');
  assert.deepEqual(new Set(result.configOwners), new Set(['gateway', 'workspace', 'defaults']));
  assert.equal((await runtimeSlot('workspace-preference', active.workspace_id)).instance.instance_id, existingBeforeDefaults.instance.instance_id);
  assert.equal((await runtimeSlot('gateway-preference')).instance.instance_id, gatewayBeforeDefaults.instance.instance_id);

  result.phase = 'create-from-defaults';
  // 使用真实创建入口生成目录、身份、创建 intent 和配置交接，不由测试预写绑定。
  const createWorkspace = async (rootPath, name) => {
    const directory = await api('/api/gateway/managed-workspaces', { method: 'POST',
      body: { root_path: rootPath, name, create_directory: true } });
    const created = directory.items.find(item => item.name === name && item.gateway_id === active.gateway_id);
    assert(created, JSON.stringify(directory));
    return created;
  };
  const workspaceSource = workspaceId => api(`/api/v1/workspaces/${workspaceId}/config/sources/workspace_root_mutable_override`,
    { headers: { 'X-BoxTeam-Workspace-Id': workspaceId } });
  const created = await createWorkspace(input.newWorkspacePath, 'Plugin Defaults Created');
  result.newWorkspace = { gateway_id: created.gateway_id, workspace_id: created.workspace_id };
  await expect.poll(async () => (await runtimeSlot('workspace-preference', created.workspace_id))?.state,
    { timeout: 30_000 }).toBe('ready');
  const createdRuntime = await runtimeSlot('workspace-preference', created.workspace_id);
  await callRuntime(createdRuntime);
  assert.notEqual(createdRuntime.instance.instance_id, existingBeforeDefaults.instance.instance_id);
  assert.notEqual(createdRuntime.instance.process_id, existingBeforeDefaults.instance.process_id);
  assert.deepEqual((await workspaceSource(created.workspace_id)).effective.plugins.runtime.entries,
    [{ package_id: 'ui.preference-probe', plugin_id: 'workspace-preference', package_version: '1.0.0', enabled: true }]);
  assert.equal((await workspaceSource(active.workspace_id)).jsonc, workspaceDocument.jsonc);
  assert.equal((await runtimeSlot('workspace-preference', active.workspace_id)).instance.instance_id, existingBeforeDefaults.instance.instance_id);
  result.defaultsCreatedIndependentInstance = true;

  await unbind('工作区偏好 · workspace-preference', 'gateway_mutable_override', '确认保存 Gateway 新建工作区默认模板', undefined, 'defaults');
  assert.equal((await runtimeSlot('workspace-preference', created.workspace_id)).instance.instance_id, createdRuntime.instance.instance_id);
  const later = await createWorkspace(input.laterWorkspacePath, 'Plugin Defaults Cleared');
  assert.deepEqual((await workspaceSource(later.workspace_id)).effective.plugins.runtime.entries, []);
  assert.equal(await runtimeSlot('workspace-preference', later.workspace_id), undefined);
  result.clearedDefaultsOnlyAffectFuture = true;

  result.phase = 'unbind-and-uninstall';
  await view.getByLabel('插件管理范围').selectOption('gateway');
  await expect(view).toContainText('Gateway 偏好 · gateway-preference');
  await unbind('Gateway 偏好 · gateway-preference', 'gateway_mutable_override', '确认保存 Gateway 插件绑定');
  await view.getByLabel('插件管理范围').selectOption('workspace');
  await pick(workspaceName);
  await unbind('工作区偏好 · workspace-preference', 'workspace_root_mutable_override', '确认保存工作区插件绑定');
  const createdReference = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/service-plugins/uninstall'));
  await packageRowFor('1.0.0').getByRole('button', { name: '卸载整个包', exact: true }).click();
  await pick('卸载整包');
  const createdRejection = await createdReference;
  assert.equal(createdRejection.status(), 409);
  const createdConflict = await createdRejection.json();
  assert.equal(createdConflict.request_id, createdRejection.headers()['x-request-id']);
  assert.match(JSON.stringify(createdConflict), new RegExp(`workspace-configuration:${created.workspace_id}:workspace-preference`));
  await callRuntime(createdRuntime);
  result.createdWorkspaceBlockedUninstall = true;
  await view.getByLabel('插件管理范围').selectOption('gateway');
  await view.getByLabel('插件管理范围').selectOption('workspace');
  await pick(created.name);
  await unbind('工作区偏好 · workspace-preference', 'workspace_root_mutable_override', '确认保存工作区插件绑定', created.workspace_id);
  await expect.poll(() => runtimeSlot('workspace-preference', created.workspace_id)).toBeUndefined();
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
  const uninstalled = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/service-plugins/uninstall'));
  await packageRow.getByRole('button', { name: '卸载整个包', exact: true }).click();
  await expect(quick).toContainText(active.gateway_id);
  await pick('卸载整包');
  const uninstallResponse = await uninstalled;
  assert.equal(uninstallResponse.status(), 200);
  const uninstallReceipt = await uninstallResponse.json();
  assert.equal(uninstallReceipt.data.uninstalled, true);
  assert.equal(uninstallReceipt.data.package_id, 'ui.preference-probe');
  assert.equal(uninstallReceipt.data.package_version, '1.0.0');
  await expect(page.locator('.notifications-toasts')).toContainText(
    `已从 原生 Gateway ${active.gateway_id} 卸载 ui.preference-probe@1.0.0`);
  await expect(view).toContainText('尚无插件包', { timeout: 30_000 });
  assert.equal(await runtimeSlot('gateway-preference'), undefined);
  assert.equal(await runtimeSlot('workspace-preference', active.workspace_id), undefined);
  result.exitedPids.push(gatewayReady.instance.process_id, workspaceRestart.instance.process_id);
  result.actualRuntimeLoop = true;
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
  const reopenedStatusStream = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/status/events');
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
  const reopenedStatusResponse = await reopenedStatusStream;
  assert.equal(reopenedStatusResponse.status(), 200);
  assert.match(reopenedStatusResponse.headers()['content-type'] ?? '', /^text\/event-stream/);
  const releasedStreamRequest = reopenedStatusResponse.request();
  const terminalCount = pluginStatusStreamTerminations.length;
  await view.getByLabel('插件管理范围').selectOption('defaults');
  await expect(view.getByLabel('插件管理范围')).toHaveValue('defaults');
  await expect.poll(() => pluginStatusStreamTerminations.length, { timeout: 15_000 }).toBeGreaterThan(terminalCount);
  const termination = pluginStatusStreamTerminations.at(-1);
  assert(termination);
  assert.equal(termination.request, releasedStreamRequest);
  const releaseError = termination.request.failure()?.errorText ?? '';
  result.runtimeStatusSubscription = { path: new URL(termination.request.url()).pathname,
    method: termination.request.method(), responseStatus: reopenedStatusResponse.status(),
    contentType: reopenedStatusResponse.headers()['content-type'], releasedOnScope: 'defaults',
    terminalEvent: termination.event, closedOnScopeChange: true, failure: releaseError };
  if (termination.event === 'requestfailed') assert.match(releaseError, /ERR_ABORTED/);
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
