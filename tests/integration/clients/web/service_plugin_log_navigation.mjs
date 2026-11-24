// Integration：真实插件和 Gateway 日志；只为缺失声明入口替换一次库存展示，不替换日志响应。
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { selectPluginScope } from './service_plugin_controls.mjs';

const input = JSON.parse(process.env.BOXTEAM_PLUGIN_LOG_UI_FIXTURE);
assert(input.artifacts && input.resultPath, '必须显式设置正式测试产物目录');
const result = { phase: 'initialization', navigationOnly: process.env.BOXTEAM_PLUGIN_LOG_UI_NAVIGATION_ONLY === '1',
  pageErrors: [], externalRequests: [], requests: [],
  boundaries: ['SSH 执行与隧道使用原 loopback 装具', '管理视图缺失声明场景只删除真实库存响应的 packages；日志与实例保持真实'] };
let browser, page, releaseLocate;
let hideInventory = false, holdLocate = false;
let locateSeen;
const locateHeld = new Promise(resolve => { locateSeen = resolve; });
const locateReleased = new Promise(resolve => { releaseLocate = resolve; });
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  const context = await browser.newContext({ viewport: { width: 1500, height: 1050 }, permissions: ['clipboard-read', 'clipboard-write'] });
  page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(input.baseUrl).origin) result.externalRequests.push(request.url());
    if (url.pathname.startsWith('/api/gateway/diagnostics')) result.requests.push({ path: url.pathname,
      connection_id: url.searchParams.get('gateway_connection_id'), log_id: url.searchParams.get('log_id'),
      gateway_id: request.headers()['x-boxteam-target-gateway-id'], include_tail: url.searchParams.get('include_tail') });
  });
  await page.route('**/api/gateway/service-plugins/inventory', async route => {
    if (!hideInventory || route.request().postDataJSON().gateway_id !== input.gatewayB) return route.continue();
    const response = await route.fetch(), body = await response.json();
    body.data.packages = [];
    await route.fulfill({ response, json: body });
  });
  await page.route('**/api/gateway/diagnostics/locate?*', async route => {
    if (!holdLocate) return route.continue();
    holdLocate = false;
    // 原 owner 已产生真实定位响应；只延迟交付浏览器，以验证切目标后的旧结果废弃。
    const response = await route.fetch();
    result.heldLocationStatus = response.status();
    locateSeen();
    await locateReleased;
    await route.fulfill({ response });
    result.heldLocationDelivered = true;
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.monaco-workbench')).toBeVisible();
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (path, options = {}, target) => page.evaluate(async ({ path, options, target }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...(target ? { 'X-BoxTeam-Target-Gateway-Id': target } : {}) },
      body: options.body && JSON.stringify(options.body) });
    const body = await response.json();
    if (!response.ok || response.headers.get('X-Request-ID') !== body.request_id) throw new Error(`${path}: ${response.status} ${JSON.stringify(body)}`);
    return body;
  }, { path, options, target });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces'), workspace = await api('/api/v1/workspace');
  assert.equal(health.data.gateway_id, input.gatewayA);
  assert.equal(workspaces.data.active_workspace.gateway_id, input.gatewayA);
  assert.equal(workspaces.data.active_workspace.workspace_id, input.workspaceId);
  assert.equal(workspace.data.workspace_id, input.workspaceId);
  result.initialization = { gateway_id: input.gatewayA, workspace_id: input.workspaceId,
    request_ids: [health.request_id, workspaces.request_id, workspace.request_id] };
  const targetA = { gateway_id: input.gatewayA, log_id: input.slots[0].log_id };
  const targetB = { gateway_id: input.gatewayB, gateway_connection_id: input.connectionId, log_id: input.slots[1].log_id };
  assert(targetA.log_id && targetB.log_id && targetA.log_id !== targetB.log_id);
  const panel = page.locator('.part.panel'), output = panel.locator('.output-view .monaco-editor');
  const outputChannel = panel.getByRole('toolbar', { name: '输出操作', exact: true }).getByLabel('切换输出', { exact: true });
  const readResponse = target => page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === '/api/gateway/diagnostics' && url.searchParams.get('log_id') === target.log_id
      && url.searchParams.get('include_tail') !== 'false' && response.request().headers()['x-boxteam-target-gateway-id'] === target.gateway_id;
  });
  const verifyRead = async (response, target) => {
    assert.equal(response.status(), 200);
    const body = await response.json(), url = new URL(response.url());
    assert.equal(response.headers()['x-request-id'], body.request_id);
    assert.equal(body.data.gateway_id, target.gateway_id);
    assert.equal(body.data.selected_log_id, target.log_id);
    if (target.gateway_connection_id) assert.equal(url.searchParams.get('gateway_connection_id'), target.gateway_connection_id);
    const log = body.data.logs.find(item => item.log_id === target.log_id);
    assert.equal(log.source, 'gateway');
    assert.equal(log.status, 'available');
    assert(log.tail.includes('Uvicorn running'));
    await expect(output).toBeVisible();
    await command('boxteam.output.focus');
    await page.keyboard.press('ControlOrMeta+End');
    const sample = log.tail.trimEnd().split('\n').at(-1).slice(0, 120).replace(/\s/g, '');
    await expect.poll(async () => (await output.locator('.view-lines').innerText()).replace(/\s/g, '')).toContain(sample);
    assert.equal(await outputChannel.inputValue(), log.label);
    return { gateway_id: body.data.gateway_id, log_id: log.log_id, request_id: body.request_id,
      label: log.label, connection_id: url.searchParams.get('gateway_connection_id'), source: log.source,
      tail_bytes: new TextEncoder().encode(log.tail).length };
  };

  if (!result.navigationOnly) {
    result.phase = 'plugin-detail-log';
    await command('workbench.view.extensions');
    const view = page.locator('.service-plugin-packages-view');
    await expect(view).toBeVisible();
    await view.getByRole('combobox', { name: '管理 Gateway', exact: true }).selectOption({ label: input.remoteName });
    await selectPluginScope(page, 'gateway');
    await view.getByRole('listitem', { name: '目标归属 Echo', exact: true }).click();
    const details = page.locator('.service-plugin-editor:visible');
    await expect(details).toBeVisible();
    await details.locator('.service-plugin-diagnostics > summary').click();
    const detailRead = readResponse(targetB);
    await details.getByRole('button', { name: '查看实例日志', exact: true }).click();
    result.detailLog = await verifyRead(await detailRead, targetB);
    assert.equal(await details.getByLabel('插件实例日志').count(), 0);

    result.phase = 'management-missing-declaration-log';
    hideInventory = true;
    await command('workbench.extensions.action.refreshExtension');
    const missing = view.locator(`[data-plugin-runtime-row][data-package-id="${input.packageId}"][data-plugin-id="${input.pluginId}"]`);
    await expect(missing.getByRole('button', { name: '查看实例日志', exact: true })).toBeVisible();
    const managementRead = readResponse(targetB);
    await missing.getByRole('button', { name: '查看实例日志', exact: true }).click();
    result.managementLog = await verifyRead(await managementRead, targetB);
    hideInventory = false;

    result.phase = 'settings-log';
    await command('boxteam.gateway.diagnostics', { gateway_id: input.gatewayB, gateway_connection_id: input.connectionId,
      name: input.remoteName, connection_kind: 'remote_gateway' });
    const diagnostics = page.getByTestId('gateway-diagnostics-panel');
    await expect(diagnostics).toBeVisible();
    const metadata = await api(`/api/gateway/diagnostics?gateway_connection_id=${input.connectionId}&include_tail=false`, {}, input.gatewayB);
    const descriptor = metadata.data.logs.find(log => log.log_id === targetB.log_id);
    assert(descriptor);
    const row = diagnostics.locator('.gateway-diagnostics-log-list > div').filter({ has: page.getByText(descriptor.label, { exact: true }) });
    const settingsRead = readResponse(targetB);
    await row.getByRole('button', { name: '在输出中查看日志', exact: true }).click();
    result.settingsLog = await verifyRead(await settingsRead, targetB);
    assert.equal(await diagnostics.locator('.gateway-diagnostics-viewer').count(), 0);

  }

  if (result.navigationOnly) {
    result.phase = 'output-readiness';
    const metadataReady = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === '/api/gateway/diagnostics' && !url.searchParams.has('log_id')
        && response.request().headers()['x-boxteam-target-gateway-id'] === input.gatewayA;
    });
    const [ready] = await Promise.all([metadataReady, command('boxteam.output.focus')]);
    const metadata = await ready.json();
    assert.equal(ready.status(), 200);
    assert.equal(ready.headers()['x-request-id'], metadata.request_id);
    assert.equal(metadata.data.gateway_id, input.gatewayA);
    await expect(output).toBeVisible();
    result.outputReadiness = { gateway_id: input.gatewayA, request_id: metadata.request_id };
  }

  result.phase = 'real-query-and-locate';
  const marker = `LOG_NAV_${input.slots[1].instance.instance_id}`;
  await api(`/api/gateway/service-plugins/${input.packageId}/${input.pluginId}/instances/${input.slots[1].instance.instance_id}/services/echo/${marker}?package_version=1.0.0`, {}, input.gatewayB);
  const scope = { source: 'gateway', workspace_ids: [] };
  const query = await api(`/api/gateway/diagnostics/query?gateway_connection_id=${input.connectionId}`,
    { method: 'POST', body: { scope, query: marker, limit: 50 } }, input.gatewayB);
  assert.equal(query.data.gateway_id, input.gatewayB);
  const match = query.data.items.find(item => item.reference.log_id === targetB.log_id);
  assert(match, '真实插件日志必须包含刚完成的业务访问记录');
  const location = { scope, reference: match.reference };
  const locatedResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/diagnostics/locate');
  const [located] = await Promise.all([locatedResponse, command('boxteam.output.openLog', { ...targetB, location })]);
  const envelope = await located.json();
  assert.equal(located.status(), 200);
  assert.equal(located.headers()['x-request-id'], envelope.request_id);
  assert.equal(new URL(located.url()).searchParams.get('gateway_connection_id'), input.connectionId);
  assert.deepEqual(envelope.data.reference, match.reference);
  const expectedSelection = Array.from(envelope.data.text).slice(match.reference.start_codepoint, match.reference.end_codepoint).join('');
  assert.equal(expectedSelection, marker);
  await expect(output.locator('.view-lines')).toContainText(marker);
  await command('boxteam.output.focus');
  await expect(output).toHaveClass(/focused/);
  result.selectionBeforeCopy = await output.locator('textarea').evaluate(element => ({
    focused: element.getRootNode().activeElement === element, value: element.value,
    start: element.selectionStart, end: element.selectionEnd,
  }));
  await page.keyboard.press('ControlOrMeta+c');
  let copiedSelection;
  try {
    await expect.poll(async () => {
      copiedSelection = await page.evaluate(() => navigator.clipboard.readText());
      return copiedSelection;
    }).toBe(marker);
  }
  catch (error) {
    // 复制结果单独失败仍验收独立的迟到导航；整次检查保留失败退出码。
    result.copyFailure = error.message; process.exitCode = 1;
  }
  await expect(page.locator('[id="boxteam.output.status"]')).toContainText('已选中日志记录');
  await page.waitForTimeout(3400);
  await expect.poll(() => output.locator('.view-lines').innerText()).not.toContain('Uvicorn running');
  result.location = { query: marker, query_request_id: query.request_id, locate_request_id: envelope.request_id,
    reference: match.reference, copied_selection: copiedSelection ?? null, preserved_after_poll: true };

  result.phase = 'late-location-target-change';
  holdLocate = true;
  await page.evaluate(target => {
    globalThis.oldLogNavigation = globalThis.workbench.commands.executeCommand('boxteam.output.openLog', target)
      .then(() => ({ ok: true }), error => ({ ok: false, message: error.message }));
  }, { ...targetB, location });
  await locateHeld;
  const aRead = readResponse(targetA);
  const [aResponse] = await Promise.all([aRead, command('boxteam.output.openLog', targetA)]);
  result.localLog = await verifyRead(aResponse, targetA);
  releaseLocate();
  await expect.poll(() => result.heldLocationDelivered).toBe(true);
  const oldOperation = await page.evaluate(() => globalThis.oldLogNavigation);
  assert.equal(oldOperation.ok, false);
  await expect.poll(() => outputChannel.inputValue()).toBe(result.localLog.label);
  await expect.poll(() => output.locator('.view-lines').innerText()).not.toContain(marker);
  result.lateLocation = { old_operation: oldOperation, visible_gateway_id: input.gatewayA };
  const active = (await api('/api/gateway/workspaces')).data.active_workspace;
  assert.deepEqual(active, workspaces.data.active_workspace);
  result.activeWorkspaceUnchanged = true;
  await page.screenshot({ path: `${input.artifacts}/log-navigation-ui.png`, fullPage: true });
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.externalRequests, []);
  result.phase = result.copyFailure ? 'navigation-complete-copy-failed' : 'complete';
} catch (error) {
  result.error = { message: error.message, stack: error.stack };
  process.exitCode = 1;
  if (page) await page.screenshot({ path: `${input.artifacts}/log-navigation-ui-failure.png`, fullPage: true }).catch(cause => { result.screenshotError = cause.message; });
} finally {
  releaseLocate();
  if (page) await page.unrouteAll({ behavior: 'wait' }).catch(error => { result.cleanupError = error.message; process.exitCode = 1; });
  if (browser) await browser.close().catch(error => { result.cleanupError = error.message; process.exitCode = 1; });
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
}
