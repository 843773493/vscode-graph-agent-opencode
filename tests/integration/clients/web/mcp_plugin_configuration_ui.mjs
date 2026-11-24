import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { parse, modify, applyEdits } from 'jsonc-parser';
import { pluginScopeAction } from './service_plugin_controls.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须指定浏览器产物目录');
const result = { phase: 'initialization', pageErrors: [], writes: [], initialization: [], mcpOperations: [], managementReads: [], sourceCatalogRequests: [], externalRequests: [] };
let browser;
let page;
const mcpWrites = [];
const mcpCatalogReads = [];
const mcpCatalogResponses = [];
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname.includes('/config/sources/')) result.writes.push(request.postDataJSON());
    if (request.method() === 'POST' && url.pathname.endsWith('/service-plugins/configuration-schemas')) result.managementReads.push({ kind: 'schemas', target: request.postDataJSON() });
    if (request.method() === 'GET' && /\/config\/sources\/[^/]+$/.test(url.pathname)) result.managementReads.push({ kind: 'source', path: url.pathname,
      gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    if (url.pathname.endsWith('/service-plugins/source-catalog')) result.sourceCatalogRequests.push(request.postDataJSON());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(fixture.baseUrl).origin) result.externalRequests.push(request.url());
  });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  // 首次加载正式静态包单列，不把上游 expect 的默认5秒当作普通业务交互预算。
  await expect(page.locator('.monaco-workbench')).toBeVisible({ timeout: 30000 });
  result.initialization = await page.evaluate(async owner => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const results = [];
    for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
      const response = await fetch(url, { headers: { 'X-Local-Token': credential.data.token,
        'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } });
      results.push({ url, status: response.status, requestId: response.headers.get('X-Request-ID'), body: await response.json() });
    }
    return results;
  }, fixture);
  for (const response of result.initialization) {
    assert.equal(response.status, 200, response.url);
    assert(response.requestId);
    assert.equal(response.requestId, response.body.request_id);
  }
  const registered = result.initialization[1].body.data;
  assert.equal(registered.active_workspace.workspace_id, fixture.workspaceId);
  const workspace = registered.items.find(item => item.gateway_id === fixture.gatewayId && item.workspace_id === fixture.workspaceId);
  assert(workspace);
  const quick = page.locator('.quick-input-widget');
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click();
  };
  const input = async value => {
    await expect(quick).toBeVisible();
    const field = quick.locator('input').first();
    await field.fill(value);
    await field.press('Enter');
  };
  const start = async id => {
    await page.locator('.part.panel').getByRole('button', { name: '添加 MCP 连接…', exact: true }).click();
    await input(id);
    await expect(page.getByRole('textbox', { name: '选择 MCP 服务来源', exact: true })).toBeVisible();
  };
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('boxteam.mcp.focus', { preserveFocus: true }));
  result.phase = 'cancel-add';
  await start('ui-cancelled');
  const sourcePicker = page.getByRole('textbox', { name: '选择 MCP 服务来源', exact: true });
  await sourcePicker.press('Escape');
  await expect(sourcePicker).not.toBeVisible();
  await expect(quick).not.toBeVisible();
  assert.equal(result.writes.length, 0);
  result.phase = 'plugin-service-add';
  await start('ui-plugin');
  await pick('已就绪的 MCP 插件服务');
  await pick('boxteam.mcp-stdio@0.1.1');
  await quick.getByText('启用此 Server', { exact: true }).click();
  const pluginSave = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/config/sources/workspace_root_mutable_override'));
  await pick('确认保存工作区 MCP 配置');
  const pluginResponse = await pluginSave;
  assert.equal(pluginResponse.status(), 200, await pluginResponse.text());
  assert.equal((await pluginResponse.json()).data.edit.state, 'active');
  result.phase = 'external-http-add';
  await start('ui-http');
  await pick('外部 Streamable HTTP');
  await input(fixture.endpoint);
  await pick('完成 HTTP Header');
  await pick('不使用 OAuth');
  await quick.getByText('启用此 Server', { exact: true }).click();
  const httpSave = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/config/sources/workspace_root_mutable_override'));
  await pick('确认保存工作区 MCP 配置');
  const httpResponse = await httpSave;
  assert.equal(httpResponse.status(), 200, await httpResponse.text());
  assert.equal((await httpResponse.json()).data.edit.state, 'active');
  assert.equal(result.writes.length, 2);
  for (const write of result.writes) {
    assert.equal(write.gateway_id, fixture.gatewayId);
    assert.equal(write.workspace_id, fixture.workspaceId);
  }
  const saved = JSON.parse(result.writes[1].jsonc).mcp.servers;
  assert.deepEqual(Object.keys(saved).sort(), ['ui-http', 'ui-plugin']);
  assert.equal(saved['ui-plugin'].transport, 'plugin');
  assert.equal(saved['ui-plugin'].service.workspace_id, fixture.scope === 'workspace' ? fixture.workspaceId : undefined);
  assert.equal(saved['ui-http'].url, fixture.endpoint);
  result.phase = 'original-directory';
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('boxteam.mcp.focus', { preserveFocus: true }));
  const panel = page.locator('.part.panel .mcp-servers-list').filter({ visible: true });
  await expect(panel).toBeVisible();
  await expect(panel.getByRole('listitem', { name: 'ui-plugin', exact: true })).toBeVisible();
  await expect(panel.getByRole('listitem', { name: 'ui-http', exact: true })).toBeVisible();
  result.phase = 'service-source-and-runtime';
  const pluginRow = panel.getByRole('listitem', { name: 'ui-plugin', exact: true });
  await expect(pluginRow).toContainText(fixture.scope === 'workspace' ? '工作区插件' : 'Gateway 共享插件');
  await expect(pluginRow).toContainText('就绪');
  await expect(panel.getByRole('listitem', { name: 'ui-http', exact: true })).toContainText('外部 MCP');
  const rowsBeforeFailure = await pluginRow.boundingBox();
  const unavailableMessage = 'MCP 目录暂不可用 · mcp-retained-catalog';
  const catalogRoute = url => url.pathname === '/api/v1/mcp/servers';
  await page.route(catalogRoute, route => route.fulfill({ status: 503, contentType: 'application/json',
    headers: { 'X-Request-ID': 'mcp-retained-catalog' }, body: JSON.stringify({ code: 503,
      message: unavailableMessage, request_id: 'mcp-retained-catalog' }) }));
  await page.locator('.part.panel').getByRole('button', { name: '刷新 MCP 状态', exact: true }).click();
  const feedback = page.locator('.boxteam-mcp-connections .message-container');
  await expect(feedback).toContainText(unavailableMessage);
  await expect(pluginRow).toBeVisible();
  const rowsDuringFailure = await pluginRow.boundingBox();
  assert.equal(rowsDuringFailure.y, rowsBeforeFailure.y, '读取失败不移动已有列表');
  await pluginRow.click();
  const failedDetails = page.locator('.mcp-server-editor:visible');
  await failedDetails.getByRole('button', { name: '管理所属插件', exact: true }).click();
  const pluginEditor = page.locator('.service-plugin-editor:visible');
  await expect(pluginEditor).toHaveAttribute('data-package-id', 'boxteam.mcp-stdio');
  await expect(pluginEditor).toHaveAttribute('data-scope', fixture.scope);
  await expect(pluginEditor).toHaveAttribute('data-workspace-id', fixture.scope === 'workspace' ? fixture.workspaceId : '');
  await expect(pluginEditor.locator('.subtitle-entry.last-non-empty')).toHaveAttribute('title', `${fixture.gatewayId} · ${fixture.scope === 'workspace' ? fixture.workspaceId : 'gateway'}`);
  const originalRuntimeIdentity = await pluginEditor.locator('.service-plugin-owner').innerText();
  await page.screenshot({ path: path.join(artifacts, 'mcp-plugin-management.png') });
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.closeActiveEditor'));
  await page.unroute(catalogRoute);
  await page.locator('.part.panel').getByRole('button', { name: '刷新 MCP 状态', exact: true }).click();
  await expect(feedback).not.toBeVisible();
  await expect(pluginRow).toContainText('连接可用');
  result.mcpServiceManagement = { source: fixture.scope, retainedOnError: true, stableLayout: true, pluginEntryWhileUnavailable: true };
  await page.screenshot({ path: path.join(artifacts, 'mcp-connections.png') });
  if (fixture.managementOnly) {
    // 此场景独立验收连接与实例边界；完整来源搜索／设置验收仍由下方原场景负责。
    const editor = page.locator('.mcp-server-editor:visible');
    for (const [label, action] of [['断开连接', 'disconnect'], ['连接', 'connect']]) {
      const operationResponse = page.waitForResponse(response => response.request().method() === 'POST'
        && new URL(response.url()).pathname === '/api/v1/mcp/connection-operations');
      await editor.getByRole('button', { name: label, exact: true }).click();
      const response = await operationResponse;
      assert.equal(response.status(), 200, await response.text());
      const envelope = await response.json();
      assert.equal(envelope.request_id, response.headers()['x-request-id']);
      assert.equal(envelope.target.gateway_id, fixture.gatewayId);
      assert.equal(envelope.target.workspace_id, fixture.workspaceId);
      assert.equal(envelope.data.action, action);
      result.mcpOperations.push(envelope.data);
      if (action === 'disconnect') {
        await expect(editor.getByRole('button', { name: '连接', exact: true })).toBeEnabled();
        await expect(pluginRow).toContainText('已就绪');
        await editor.getByRole('button', { name: '管理所属插件', exact: true }).click();
        await expect(pluginEditor.locator('.service-plugin-owner')).toHaveText(originalRuntimeIdentity);
        await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.closeActiveEditor'));
      } else await expect(pluginRow).toContainText('连接可用');
    }
    result.mcpServiceManagement.runtimeSurvivesDisconnect = true;
    await editor.getByRole('button', { name: '配置来源', exact: true }).click();
    const connectionSettings = page.locator('.settings-editor:visible');
    await expect(connectionSettings.locator('.settings-context-label')).toContainText(fixture.gatewayId);
    await expect(connectionSettings.locator('.settings-context-label')).toContainText(fixture.workspaceId);
    assert(result.managementReads.some(item => item.kind === 'source' && item.gatewayId === fixture.gatewayId
      && item.workspaceId === fixture.workspaceId && item.path.endsWith('/workspace_root_mutable_override')));
    result.mcpServiceManagement.connectionSettingsOwner = { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId };
    await page.screenshot({ path: path.join(artifacts, 'mcp-connection-settings.png') });
    await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.closeActiveEditor'));
    await page.screenshot({ path: path.join(artifacts, 'mcp-connections-final.png') });
  } else {
  result.phase = 'resource-quick-access';
  const resourceOwnerReads = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname.includes('/api/v1/mcp/')) mcpWrites.push(request.url());
    if (request.method() === 'GET' && url.pathname === '/api/v1/mcp/servers'
      && url.searchParams.get('refresh') === 'false') {
      const headers = request.headers();
      mcpCatalogReads.push({ phase: result.phase, gatewayId: headers['x-boxteam-target-gateway-id'],
        workspaceId: headers['x-boxteam-workspace-id'], url: request.url() });
    }
    if (result.phase === 'resource-quick-access' && request.method() === 'GET'
      && url.pathname.startsWith('/api/v1/resources')) {
      const headers = request.headers();
      resourceOwnerReads.push({ path: url.pathname, gatewayId: headers['x-boxteam-target-gateway-id'],
        workspaceId: headers['x-boxteam-workspace-id'] });
    }
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (response.request().method() !== 'GET' || url.pathname !== '/api/v1/mcp/servers'
      || url.searchParams.get('refresh') !== 'false') return;
    mcpCatalogResponses.push((async () => {
      try {
        const body = await response.json();
        return { phase: result.phase, status: response.status(), requestId: response.headers()['x-request-id'],
          gatewayId: response.request().headers()['x-boxteam-target-gateway-id'],
          workspaceId: response.request().headers()['x-boxteam-workspace-id'],
          serverIds: body.data.map(server => server.server_id) };
      } catch (error) {
        return { phase: result.phase, status: response.status(), requestId: response.headers()['x-request-id'],
          bodyError: error instanceof Error ? error.message : String(error) };
      }
    })());
  });
  const searchMcp = async () => {
    await expect(quick).toBeVisible();
    const input = quick.locator('input').first();
    await expect(input).toBeFocused({ timeout: 30000 });
    await input.fill('resource ui-plugin');
    const row = quick.locator('.monaco-list-row').filter({ hasText: 'ui-plugin' });
    await expect(row).toContainText('MCP', { timeout: 30000 });
    await expect(input).toBeFocused();
    const settledRow = quick.locator('.monaco-list-row').filter({ hasText: 'ui-plugin' });
    await expect(settledRow).toHaveCount(1);
    await expect(settledRow).toBeVisible();
    return settledRow;
  };
  const openResourcePicker = async () => {
    await page.keyboard.press('Control+Shift+P');
    await quick.locator('input').first().fill('> 查找来源资源');
    const command = quick.locator('.monaco-list-row').filter({ hasText: '查找来源资源' }).first();
    await expect(command).toBeVisible();
    await command.press('Enter');
    await expect(quick.locator('input').first()).toHaveValue('resource ');
    await quick.getByRole('button', { name: '选择来源所属工作区', exact: true }).click();
    const owner = quick.locator('.monaco-list-row').filter({ hasText: workspace.name }).first();
    await expect(owner).toBeVisible();
    await owner.press('Space');
    await expect(owner.locator('.monaco-checkbox')).toHaveAttribute('aria-checked', 'true');
    await quick.locator('input').first().press('Enter');
    await expect(quick.locator('input').first()).toHaveValue('resource ');
  };
  const findMcp = async () => { await openResourcePicker(); return searchMcp(); };
  await findMcp();
  expect(resourceOwnerReads.length).toBeGreaterThan(0);
  expect(resourceOwnerReads.every(request => request.gatewayId === fixture.gatewayId
    && request.workspaceId === fixture.workspaceId)).toBe(true);
  await expect(quick.locator('.monaco-list-row.focused')).toContainText('ui-plugin');
  await quick.locator('input').first().press('Enter');
  await expect(page.locator('.tab').filter({ hasText: 'MCP 服务器: ui-plugin' })).toBeVisible();
  const details = page.locator('.mcp-server-editor').filter({ visible: true });
  await expect(details).toContainText('ui-plugin'); await expect(details).toContainText(workspace.name);
  const readmeFrame = () => {
    const shell = page.frames().find(frame => frame.url().includes('/contrib/webview/browser/pre/index.html'));
    return shell && page.frames().find(frame => frame.parentFrame() === shell);
  };
  const readmeText = async () => {
    const frame = readmeFrame();
    if (!frame) return '';
    try {
      return await frame.locator('body').innerText();
    } catch (error) {
      if (error instanceof Error && error.message.includes('Frame was detached')) return '';
      throw error;
    }
  };
  assert.equal(result.writes.length, 2);
  result.mcpQuickAccess = true;
  result.phase = 'resource-quick-access-lifecycle';
  const manage = async () => {
    result.actionStage = 'manage-open-picker';
    const row = await findMcp();
    const moreActions = row.getByRole('button', { name: '更多 MCP 动作', exact: true });
    await expect(moreActions).toBeEnabled();
    await row.hover();
    await expect(moreActions).toBeVisible();
    // 目录到齐会替换原行；locator 会重新取得节点并核可见、稳定和真实鼠标命中。
    await moreActions.click();
    await expect(quick.getByText('显示详情', { exact: true })).toBeVisible();
  };
  const submit = async (action, label) => {
    await manage();
    result.actionStage = `${action}-choose-item`;
    const choice = quick.locator('.monaco-list-row').filter({ hasText: label });
    await expect(choice).toHaveCount(1);
    await expect(choice).toBeVisible();
    result.actionStage = `${action}-click-item`;
    const choiceLabel = choice.getByText(label, { exact: true });
    await choiceLabel.click({ trial: true });
    const choicePoint = await choiceLabel.evaluate(element => {
      const rect = element.getBoundingClientRect(), target = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2,
        hitLabel: target === element || element.contains(target) };
    });
    assert.equal(choicePoint.hitLabel, true, '真实鼠标目标必须是所选 MCP 动作');
    const pending = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/mcp/connection-operations');
    await page.mouse.click(choicePoint.x, choicePoint.y);
    result.actionStage = `${action}-await-operation-response`;
    const response = await pending;
    assert.equal(response.status(), 200, await response.text());
    const body = await response.json();
    assert.equal(response.headers()['x-request-id'], body.request_id);
    assert(body.request_id);
    assert.equal(body.target.gateway_id, fixture.gatewayId);
    assert.equal(body.target.workspace_id, fixture.workspaceId);
    const headers = await response.request().allHeaders();
    assert.equal(headers['x-boxteam-target-gateway-id'], fixture.gatewayId);
    assert.equal(headers['x-boxteam-workspace-id'], fixture.workspaceId);
    assert.equal(body.data.server_id, 'ui-plugin');
    assert.equal(body.data.action, action);
    result.mcpOperations.push(body.data);
    const operation = async () => {
      const result = await page.evaluate(async ({ baseUrl, gatewayId, workspaceId, operationId }) => {
        const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
        const response = await fetch(`${baseUrl}/api/v1/mcp/connection-operations/${operationId}`, {
          headers: { 'X-Local-Token': credential.data.token,
            'x-boxteam-target-gateway-id': gatewayId, 'x-boxteam-workspace-id': workspaceId },
        });
        return { status: response.status, requestId: response.headers.get('x-request-id'), envelope: await response.json() };
      }, { baseUrl: fixture.baseUrl, gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId, operationId: body.data.operation_id });
      assert.equal(result.status, 200, JSON.stringify(result.envelope));
      const envelope = result.envelope;
      assert.equal(envelope.request_id, result.requestId);
      assert.equal(envelope.target.gateway_id, fixture.gatewayId);
      assert.equal(envelope.target.workspace_id, fixture.workspaceId);
      return envelope.data;
    };
    await expect.poll(async () => (await operation()).status, { timeout: 30000 }).toBe('succeeded');
    result.mcpOperations[result.mcpOperations.length - 1] = await operation();
    await expect.poll(readmeText, { timeout: 30000 }).toContain(body.data.operation_id);
    await expect.poll(readmeText, { timeout: 30000 }).toContain('操作已完成');
    await expect(panel.getByRole('listitem', { name: 'ui-http', exact: true })).toContainText('连接可用');
  };
  await submit('disconnect', '断开连接');
  await expect.poll(readmeText, { timeout: 30000 }).not.toContain('连接可用');
  await manage();
  await expect(quick.getByText('连接', { exact: true })).toBeVisible();
  await expect(quick.getByText('断开连接', { exact: true })).toHaveCount(0);
  await quick.locator('input').first().press('Escape');
  await expect(quick).not.toBeVisible();
  assert.equal(mcpWrites.length, 1, '取消已停止服务的动作选择不能提交连接');
  await submit('connect', '连接');
  await expect.poll(readmeText, { timeout: 30000 }).toContain('连接可用');
  assert.equal(mcpWrites.length, 2, '每次接受只提交一次原连接操作，关闭浮层不重放');
  assert.equal(result.writes.length, 2, '连接操作不另写MCP配置');
  result.mcpQuickAccessLifecycle = true;

  result.phase = 'management-entry-wiring';
  await expect.poll(readmeText).toMatch(/MCP\s+resources\s+和\s+templates\s+浏览尚未提供/);
  await expect.poll(readmeText).toContain('不重启或停用插件实例');
  await expect(details.getByRole('button', { name: '配置来源', exact: true })).toBeVisible();
  const pluginParameters = details.getByRole('button', { name: '插件参数与私有输入', exact: true });
  await expect(pluginParameters).toBeVisible();
  const schemaStart = result.managementReads.length;
  await pluginParameters.click();
  await expect(quick).toContainText('查看参数 schema');
  await expect(quick).toContainText('boxteam.mcp-stdio/stdio@0.1.1');
  await quick.locator('input').first().press('Escape');
  await expect(quick).not.toBeVisible();
  assert.deepEqual(result.managementReads.slice(schemaStart).filter(item => item.kind === 'schemas').map(item => item.target), [{
    gateway_id: fixture.gatewayId, ...(fixture.scope === 'workspace' ? { workspace_id: fixture.workspaceId } : {}),
  }]);
  const sourceStart = result.managementReads.length;
  await details.getByRole('button', { name: '配置来源', exact: true }).click();
  const settings = page.locator('.settings-editor:visible');
  await expect(settings.locator('.settings-context-label')).toContainText(fixture.gatewayId);
  await expect(settings.locator('.settings-context-label')).toContainText(fixture.workspaceId);
  await settings.locator('.settings-context-label summary').click();
  await expect(settings.locator('.settings-context-label').getByRole('combobox', { name: '配置来源', exact: true }))
    .toHaveValue('工作区设置');
  assert(result.managementReads.slice(sourceStart).some(item => item.kind === 'source' && item.gatewayId === fixture.gatewayId
    && item.workspaceId === fixture.workspaceId && item.path.endsWith('/workspace_root_mutable_override')));
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.closeActiveEditor'));
  await manage();
  await expect(quick.getByText('配置来源', { exact: true })).toBeVisible();
  await expect(quick.getByText('插件参数与私有输入', { exact: true })).toBeVisible();
  await quick.locator('input').first().press('Escape');
  assert.equal(result.writes.length, 2, '查看及取消管理入口不能提交配置');
  result.mcpManagementEntries = { owner: { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId },
    parameterScope: fixture.scope, exactVersion: '0.1.1', cancelledWithoutWrite: true, resourcesTemplatesUnavailable: true };

  result.phase = 'download-source-management';
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
  const packages = page.locator('.service-plugin-packages-view');
  await expect(packages).toContainText('空源不会访问第三方市场');
  const catalogsBefore = result.sourceCatalogRequests.length, externalBefore = result.externalRequests.length;
  const install = await pluginScopeAction(page, '安装插件包');
  await install.click({ delay: 150 });
  const emptyInventory = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/service-plugins/inventory'));
  await pick('自有来源');
  const emptyResponse = await emptyInventory;
  assert.equal(emptyResponse.status(), 200, await emptyResponse.text());
  assert.equal((await emptyResponse.json()).data.sources.configured, false);
  await expect(page.locator('.notifications-toasts')).toContainText('尚未启用插件下载源');
  await expect(quick).not.toBeVisible();
  assert.equal(result.sourceCatalogRequests.length, catalogsBefore);
  assert.equal(result.externalRequests.length, externalBefore);
  const sourceAction = await pluginScopeAction(page, '管理插件下载源');
  await sourceAction.click({ delay: 150 });
  await pick('gateway_mutable_override');
  await expect(settings).toBeVisible();
  await expect(settings.locator('.settings-context-label')).toContainText(fixture.gatewayId);
  await expect(settings.locator('.settings-category-context select[aria-label="配置范围"] option:checked')).toHaveText('Gateway 设置');
  await expect(settings.locator('.search-container .view-lines')).toHaveText('plugins.sources.entries');
  await expect(settings.locator('[data-key="plugins.sources.entries"]')).toBeVisible();
  const api = (url, options = {}) => page.evaluate(async ({ url, options, gatewayId }) => {
    const auth = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { ...options, headers: { 'X-Local-Token': auth.token, 'X-BoxTeam-Target-Gateway-Id': gatewayId,
      'Content-Type': 'application/json' }, body: options.body && JSON.stringify(options.body) });
    const envelope = await response.json();
    if (!response.ok || envelope.code !== 0 || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return envelope.data;
  }, { url, options, gatewayId: fixture.gatewayId });
  const sourcePath = '/api/gateway/config/sources/gateway_mutable_override';
  const originalSource = await api(sourcePath);
  const entries = [{ source_id: 'management-disabled', protocol_version: 1, enabled: false }];
  const changedJsonc = applyEdits(originalSource.jsonc, modify(originalSource.jsonc, ['settings', 'gateway', 'plugins', 'sources', 'entries'], entries,
    { formattingOptions: { insertSpaces: true, tabSize: 2 } }));
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('settings.switchToJSON'));
  const text = page.locator('.editor-instance:visible .monaco-editor .view-lines').last();
  await expect(text).toBeVisible();
  await text.click(); await page.keyboard.press('Control+Home'); await page.keyboard.press('Control+A');
  await page.evaluate(value => navigator.clipboard.writeText(value), changedJsonc);
  await page.keyboard.press('Control+V');
  await expect(page.locator('.tabs-container .tab.active:visible').last()).toHaveClass(/\bdirty\b/);
  const saving = page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.files.save'));
  const dialog = page.locator('.monaco-dialog-box[role="dialog"]');
  await expect(dialog).toContainText('保存目标配置来源');
  await dialog.getByRole('button', { name: '保存来源', exact: true }).click();
  await saving;
  const savedSource = await api(sourcePath);
  assert.deepEqual(parse(savedSource.jsonc).settings.gateway.plugins.sources.entries, entries);
  assert.deepEqual(savedSource.source_effective.plugins.sources.entries, entries);
  const sourceWrite = result.writes.at(-1);
  assert.equal(result.writes.length, 3);
  assert.equal(sourceWrite.gateway_id, fixture.gatewayId);
  assert.equal(sourceWrite.workspace_id, undefined);
  assert.equal(sourceWrite.source_key, 'gateway_mutable_override');
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
  await expect(packages).toContainText('尚未启用插件下载源');
  assert.equal(result.sourceCatalogRequests.length, catalogsBefore);
  assert.equal(result.externalRequests.length, externalBefore);
  result.downloadSourceManagement = { owner: { gatewayId: fixture.gatewayId }, sourceKey: sourceWrite.source_key,
    persistedEntries: entries, originalWriter: true, emptySourceCatalogRequests: 0, emptySourceExternalRequests: 0 };
  await page.screenshot({ path: path.join(artifacts, 'plugin-mcp-management.png'), fullPage: true });
  }
  assert.deepEqual(result.pageErrors, []);
  result.phase = 'completed';
} finally {
  try {
    result.mcpWrites = mcpWrites;
    result.mcpCatalogReads = mcpCatalogReads;
    result.mcpCatalogResponses = await Promise.all(mcpCatalogResponses);
    if (page && result.phase !== 'completed') {
      result.failureState = await page.evaluate(() => {
        const widget = document.querySelector('.quick-input-widget');
        return { quickVisible: Boolean(widget && getComputedStyle(widget).display !== 'none'),
          inputValue: widget?.querySelector('input')?.value ?? null,
          rows: [...(widget?.querySelectorAll('.monaco-list-row') ?? [])].map(row => row.textContent?.trim()),
          editorText: document.querySelector('.mcp-server-editor')?.textContent?.trim() ?? null,
          activeElement: document.activeElement?.outerHTML?.slice(0, 600) ?? null,
          quickClass: widget?.className, quickStyle: widget?.getAttribute('style') };
      });
      await page.screenshot({ path: path.join(artifacts, 'mcp-add.png'), fullPage: true });
    }
    await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  } finally {
    if (browser) await browser.close();
  }
}
