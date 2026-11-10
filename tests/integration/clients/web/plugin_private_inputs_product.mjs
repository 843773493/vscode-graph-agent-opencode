import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const input = JSON.parse(process.env.BOXTEAM_PLUGIN_PRIVATE_INPUT_FIXTURE);
const result = { phase: 'initialization', pageErrors: [], privateWrites: [], configWrites: [], pluginResults: [],
  cancellations: [], gateway_rebuilt: false, workspace_rebuilt: false, secret_values_absent: false };
const secretValues = ['gateway-product-private-sentinel', 'workspace-product-private-sentinel',
  'cancelled-secret-sentinel', 'cancelled-confirm-secret-sentinel', 'structured-private-sentinel'];
let browser;
let page;
let privateWriteCount = 0;
let configWriteCount = 0;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && (url.pathname.endsWith('/service-plugins/private-inputs') || url.pathname.endsWith('/config/plugin-private-inputs'))) {
      privateWriteCount++;
      const body = JSON.parse(request.postData() ?? '{}');
      result.privateWrites.push({ path: url.pathname, scope: body.workspace_id ? 'workspace' : 'gateway',
        package_id: body.package_id, plugin_id: body.plugin_id, source_key: body.source_key, parameter_pointer: body.parameter_pointer });
    }
    if (request.method() === 'PUT' && url.pathname.includes('/config/sources/')) {
      configWriteCount++;
      const body = request.postData() ?? '';
      result.configWrites.push({ path: url.pathname, containsSecret: secretValues.some(value => body.includes(value)) });
    }
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const api = (path, options = {}) => page.evaluate(async ({ path, options }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...(options.headers ?? {}) },
      body: options.body && JSON.stringify(options.body) });
    const envelope = await response.json();
    if (!response.ok || (envelope.request_id && envelope.request_id !== response.headers.get('X-Request-ID'))) {
      throw new Error(`${path}: ${response.status} ${JSON.stringify(envelope)}`);
    }
    return Object.hasOwn(envelope, 'data') ? envelope.data : envelope;
  }, { path, options });
  const health = await api('/api/gateway/health');
  const workspaces = await api('/api/gateway/workspaces');
  await api('/api/v1/workspace');
  const active = workspaces.active_workspace;
  assert(active?.workspace_id && active.gateway_id === health.gateway_id);
  const activeWorkspace = workspaces.items.find(item => item.workspace_id === active.workspace_id && item.gateway_id === active.gateway_id);
  assert(activeWorkspace);
  result.owner = { gateway_id: health.gateway_id, workspace_id: active.workspace_id, workspace_name: activeWorkspace.name };
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
  const view = page.locator('.service-plugin-packages-view'), quick = page.locator('.quick-input-widget');
  await expect(view).toBeVisible();
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click();
  };
  const inputQuick = async value => {
    await expect(quick).toBeVisible();
    const field = quick.locator('input').first(); await field.fill(value); await field.press('Enter');
  };
  await view.getByRole('button', { name: '安装插件包', exact: true }).click();
  await pick('ZIP 文件'); await inputQuick(input.packagePath);
  await expect(view).toContainText('product.private-inputs@1.0.0', { timeout: 30_000 });
  const selectScope = async scope => {
    await view.getByLabel('插件管理范围').selectOption(scope);
    if (scope === 'workspace') await pick(result.owner.workspace_name);
  };
  const openPlugin = async (scope, name) => {
    await selectScope(scope); await expect(view).toContainText(name);
    await view.getByRole('button', { name, exact: true }).click();
    await expect(view.locator('.service-plugin-details')).toBeVisible();
  };
  const enable = async (scope, name, source, confirm) => {
    await openPlugin(scope, name);
    await view.getByRole('button', { name: '启用此范围', exact: true }).click();
    await pick(source); await pick(confirm); await expect(quick).not.toBeVisible();
  };
  const openSecret = async (scope, name, source, pointer) => {
    await openPlugin(scope, name);
    await view.getByRole('button', { name: pointer ? '按位置管理秘密参数' : '管理秘密参数 /credential', exact: true }).click();
    await pick(source);
    if (pointer) { await expect(quick).toContainText('选择秘密参数位置'); await inputQuick(pointer); }
    await pick('设置秘密参数');
    const field = quick.locator('input[type="password"]'); await expect(field).toHaveCount(1); return field;
  };
  const setSecret = async (scope, name, source, value, pathSuffix, pointer) => {
    const field = await openSecret(scope, name, source, pointer); await field.fill(pointer ? JSON.stringify(value) : value); await field.press('Enter');
    const privatePromise = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith(pathSuffix));
    const configPromise = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname.endsWith(`/config/sources/${source}`));
    await pick('确认保存秘密');
    const privateResponse = await privatePromise, configResponse = await configPromise;
    assert.equal(privateResponse.status(), 200); assert.equal(configResponse.status(), 200);
    const privateReceipt = await privateResponse.json(), configReceipt = await configResponse.json();
    const privateRequest = result.privateWrites.at(-1);
    assert.equal(privateRequest.scope, scope);
    assert(!JSON.stringify(privateReceipt).includes(value));
    result.privateWrites.at(-1).status = privateResponse.status();
    result.privateWrites.at(-1).returnedValue = false;
    assert.equal(configReceipt.data.edit.source_saved, true);
    await expect(quick).not.toBeVisible();
  };
  result.phase = 'install';
  const sourceGateway = 'gateway_mutable_override', sourceWorkspace = 'workspace_root_mutable_override';
  await enable('gateway', 'Gateway 私有输入探针 · gateway-probe', sourceGateway, '确认保存 Gateway 插件绑定');
  const cancelled = await openSecret('gateway', 'Gateway 私有输入探针 · gateway-probe', sourceGateway);
  const privateCount = privateWriteCount, configCount = configWriteCount;
  await cancelled.fill('cancelled-secret-sentinel'); await cancelled.press('Escape'); await expect(quick).not.toBeVisible();
  result.cancellations.push({ action: 'escape', privateWrites: privateWriteCount - privateCount, configWrites: configWriteCount - configCount });
  const cancelConfirm = await openSecret('gateway', 'Gateway 私有输入探针 · gateway-probe', sourceGateway);
  await cancelConfirm.fill('cancelled-confirm-secret-sentinel'); await cancelConfirm.press('Enter'); await pick('取消'); await expect(quick).not.toBeVisible();
  result.cancellations.push({ action: 'confirm-cancel', privateWrites: privateWriteCount - privateCount, configWrites: configWriteCount - configCount });
  assert(result.cancellations.every(item => item.privateWrites === 0 && item.configWrites === 0));
  result.phase = 'save-gateway-secret';
  const gatewaySecret = secretValues[0];
  await setSecret('gateway', 'Gateway 私有输入探针 · gateway-probe', sourceGateway, gatewaySecret, '/service-plugins/private-inputs');
  await enable('workspace', '工作区私有输入探针 · workspace-probe', sourceWorkspace, '确认保存工作区插件绑定');
  result.phase = 'save-workspace-secret';
  const workspaceSecret = secretValues[1];
  await setSecret('workspace', '工作区私有输入探针 · workspace-probe', sourceWorkspace, workspaceSecret, '/config/plugin-private-inputs');
  const gatewayDocument = await api(`/api/gateway/config/sources/${sourceGateway}`);
  const workspaceDocument = await api(`/api/v1/workspaces/${active.workspace_id}/config/sources/${sourceWorkspace}`,
    { headers: { 'X-BoxTeam-Workspace-Id': active.workspace_id } });
  const gatewayReference = gatewayDocument.effective.plugins.configuration.values['product.private-inputs']['gateway-probe'].credential;
  const workspaceReference = workspaceDocument.effective.plugins.configuration.values['product.private-inputs']['workspace-probe'].credential;
  assert.equal(gatewayReference.kind, 'plugin_private_file'); assert.equal(workspaceReference.kind, 'plugin_private_file');
  assert.equal(gatewayReference.workspace_id, null); assert.equal(workspaceReference.workspace_id, active.workspace_id);
  assert.equal(gatewayReference.package_id, 'product.private-inputs'); assert.equal(gatewayReference.plugin_id, 'gateway-probe');
  assert.equal(workspaceReference.package_id, 'product.private-inputs'); assert.equal(workspaceReference.plugin_id, 'workspace-probe');
  assert(gatewayDocument.jsonc.includes(gatewayReference.version)); assert(workspaceDocument.jsonc.includes(workspaceReference.version));
  const observed = JSON.stringify({ gatewayDocument, workspaceDocument, privateWrites: result.privateWrites });
  assert(!observed.includes(gatewaySecret) && !observed.includes(workspaceSecret));
  assert.deepEqual(result.privateWrites.map(item => item.scope).sort(), ['gateway', 'workspace']);
  assert(result.privateWrites.every(item => item.status === 200 && !item.returnedValue && item.parameter_pointer === '/credential'));
  assert(result.configWrites.every(item => !item.containsSecret));
  result.secret_values_absent = true;
  result.references = { gateway: gatewayReference, workspace: workspaceReference };

  result.phase = 'structured-secret';
  await openPlugin('workspace', '工作区私有输入探针 · workspace-probe');
  await view.getByRole('button', { name: '编辑参数 JSON', exact: true }).click();
  await pick(sourceWorkspace); await pick('编辑此来源的参数');
  const parameterInput = quick.locator('input').first();
  const localParameters = JSON.parse(await parameterInput.inputValue());
  localParameters.entries = [{ label: 'first' }, { label: 'untouched' }];
  await inputQuick(JSON.stringify(localParameters));
  await pick('确认保存工作区插件参数'); await expect(quick).not.toBeVisible();
  const structuredSecret = secretValues[4], structuredPointer = '/entries/0/credential';
  await setSecret('workspace', '工作区私有输入探针 · workspace-probe', sourceWorkspace,
    structuredSecret, '/config/plugin-private-inputs', structuredPointer);
  const structuredDocument = await api(`/api/v1/workspaces/${active.workspace_id}/config/sources/${sourceWorkspace}`,
    { headers: { 'X-BoxTeam-Workspace-Id': active.workspace_id } });
  const structured = structuredDocument.effective.plugins.configuration.values['product.private-inputs']['workspace-probe'];
  assert.deepEqual(structured.credential, workspaceReference);
  assert.equal(structured.entries.length, 2); assert.deepEqual(structured.entries[1], { label: 'untouched' });
  assert.equal(structured.entries[0].credential.kind, 'plugin_private_file');
  assert.equal(structured.entries[0].credential.parameter_pointer, structuredPointer);
  assert(!JSON.stringify(structuredDocument).includes(structuredSecret));
  assert.equal(result.privateWrites.at(-1).parameter_pointer, structuredPointer);
  assert(result.privateWrites.every(item => item.status === 200 && !item.returnedValue));
  assert(result.configWrites.every(item => !item.containsSecret));
  result.structured_private_input = true;

  const gatewayTarget = { gateway_id: health.gateway_id };
  const status = (data, plugin, workspaceId) => data.plugins.find(item => item.binding.plugin_id === plugin
    && (workspaceId ? item.binding.workspace_id === workspaceId : item.binding.workspace_id == null));
  const waitStatus = async (plugin, workspaceId, predicate) => {
    const deadline = Date.now() + 60_000; let last;
    while (Date.now() < deadline) {
      last = await api('/api/gateway/service-plugins/status', { method: 'POST', body: gatewayTarget });
      const item = status(last, plugin, workspaceId); if (item && predicate(item)) return item;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    throw new Error(`插件未进入预期状态 ${plugin}: ${JSON.stringify(last)}`);
  };
  const gatewayReady = await waitStatus('gateway-probe', undefined, item => item.state === 'ready');
  const workspaceReady = await waitStatus('workspace-probe', active.workspace_id, item => item.state === 'ready');
  const call = async (item, workspaceId) => {
    const b = item.binding, instance = item.instance;
    const query = new URLSearchParams({ package_version: b.package_version, ...(workspaceId ? { workspace_id: workspaceId } : {}) });
    const path = `/api/gateway/service-plugins/${b.package_id}/${b.plugin_id}/instances/${instance.instance_id}/services/echo/value?${query}`;
    return api(path);
  };
  const gatewayValue = await call(gatewayReady), workspaceValue = await call(workspaceReady, active.workspace_id);
  const digest = value => requireDigest(value);
  assert.equal(gatewayValue.credential_digest, digest(gatewaySecret)); assert.equal(workspaceValue.credential_digest, digest(workspaceSecret));
  assert.equal(workspaceValue.nested_digest, digest(structuredSecret)); assert.deepEqual(workspaceValue.entry_labels, ['first', 'untouched']);
  result.pluginResults.push({ scope: 'gateway', credential_present: gatewayValue.credential_present, secretReturned: JSON.stringify(gatewayValue).includes(gatewaySecret) });
  result.pluginResults.push({ scope: 'workspace', credential_present: workspaceValue.credential_present, secretReturned: JSON.stringify(workspaceValue).includes(workspaceSecret) });
  assert(result.pluginResults.every(item => item.credential_present && !item.secretReturned));
  const restartFromWorkbench = async (scope, name) => {
    await openPlugin(scope, name);
    const completed = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith('/service-plugins/restart'));
    await view.getByRole('button', { name: '重启实例', exact: true }).click();
    const response = await completed;
    assert.equal(response.status(), 200);
    const request = JSON.parse(response.request().postData() ?? '{}');
    assert.equal(request.gateway_id, health.gateway_id);
    assert.equal(request.workspace_id ?? null, scope === 'workspace' ? active.workspace_id : null);
    assert.equal(request.plugin_id, scope === 'workspace' ? 'workspace-probe' : 'gateway-probe');
  };
  await restartFromWorkbench('gateway', 'Gateway 私有输入探针 · gateway-probe');
  await restartFromWorkbench('workspace', '工作区私有输入探针 · workspace-probe');
  const gatewayRestart = await waitStatus('gateway-probe', undefined, item => item.state === 'ready'
    && item.instance.instance_id !== gatewayReady.instance.instance_id);
  const workspaceRestart = await waitStatus('workspace-probe', active.workspace_id, item => item.state === 'ready'
    && item.instance.instance_id !== workspaceReady.instance.instance_id);
  assert.notEqual(gatewayRestart.instance.instance_id, gatewayReady.instance.instance_id);
  assert.notEqual(workspaceRestart.instance.instance_id, workspaceReady.instance.instance_id);
  const gatewayAfterRestart = await call(gatewayRestart), workspaceAfterRestart = await call(workspaceRestart, active.workspace_id);
  assert.equal(gatewayAfterRestart.credential_digest, digest(gatewaySecret)); assert.equal(workspaceAfterRestart.credential_digest, digest(workspaceSecret));
  assert.equal(workspaceAfterRestart.nested_digest, digest(structuredSecret));
  const readInstanceLog = async (scope, name, instance, otherInstance, loggedInstance = instance.instance) => {
    await openPlugin(scope, name);
    const request = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/diagnostics');
    await view.getByRole('button', { name: '查看实例日志', exact: true }).click();
    const response = await request;
    assert.equal(response.status(), 200);
    assert.equal(response.request().headers()['x-boxteam-target-gateway-id'], health.gateway_id);
    const receipt = await response.json();
    assert.equal(receipt.request_id, response.headers()['x-request-id']);
    assert.equal(receipt.data.gateway_id, health.gateway_id);
    assert.equal(new URL(response.url()).searchParams.get('log_id'), instance.log_id);
    assert.equal(receipt.data.selected_log_id, instance.log_id);
    const output = view.getByLabel('插件实例日志');
    await expect(output).toContainText(`plugin-instance-log ${instance.binding.plugin_id} ${loggedInstance.instance_id}`);
    assert(!(await output.textContent()).includes(otherInstance.instance.instance_id));
    assert(!secretValues.some(value => receipt.data.logs.some(log => log.tail?.includes(value))));
  };
  result.phase = 'instance-logs';
  await readInstanceLog('gateway', 'Gateway 私有输入探针 · gateway-probe', gatewayRestart, gatewayReady);
  result.instance_logs_gateway = true;
  await readInstanceLog('workspace', '工作区私有输入探针 · workspace-probe', workspaceRestart, gatewayRestart);
  result.instance_logs_workspace = true;
  await openPlugin('gateway', 'Gateway 私有输入探针 · gateway-probe');
  await view.getByRole('button', { name: '停用此范围', exact: true }).click();
  await pick(sourceGateway); await pick('确认保存 Gateway 插件绑定'); await expect(quick).not.toBeVisible();
  const stopped = await waitStatus('gateway-probe', undefined, item => item.state === 'stopped' && !item.binding.enabled);
  assert.equal(stopped.log_id, gatewayRestart.log_id);
  assert.equal(stopped.instance, null);
  await readInstanceLog('gateway', 'Gateway 私有输入探针 · gateway-probe', stopped, workspaceRestart, gatewayRestart.instance);
  result.stopped_instance_logs = true;
  await openPlugin('workspace', '工作区私有输入探针 · workspace-probe');
  await view.getByRole('button', { name: '按位置管理秘密参数', exact: true }).click();
  await pick(sourceWorkspace); await expect(quick).toContainText('选择秘密参数位置');
  await inputQuick(structuredPointer); await pick('清除此来源的秘密覆盖');
  await pick('确认保存工作区插件秘密参数'); await expect(quick).not.toBeVisible();
  const cleared = await waitStatus('workspace-probe', active.workspace_id, item => item.state === 'ready'
    && item.instance.instance_id !== workspaceRestart.instance.instance_id);
  const clearedValue = await call(cleared, active.workspace_id);
  assert.equal(clearedValue.nested_digest, digest('')); assert.deepEqual(clearedValue.entry_labels, ['first', 'untouched']);
  assert.equal(clearedValue.credential_digest, digest(workspaceSecret));
  assert(result.configWrites.every(item => !item.containsSecret));
  result.structured_private_cleared = true;
  result.gateway_rebuilt = true; result.workspace_rebuilt = true; result.phase = 'complete';
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
} catch (error) {
  result.failure = error instanceof Error ? error.stack : String(error);
  if (page) {
    result.startupError = await page.locator('#startup-error').textContent().catch(() => null);
    try { await page.screenshot({ path: `${input.resultPath}.png` }); } catch (diagnosticError) { result.screenshotError = String(diagnosticError); }
  }
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
  throw error;
} finally { await browser?.close(); }

function requireDigest(value) {
  // 该消化值只用于测试秘密未被插件 HTTP 响应回显。
  return createHash('sha256').update(value).digest('hex');
}
