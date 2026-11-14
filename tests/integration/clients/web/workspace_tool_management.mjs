import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_TOOLS_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], requests: [], changes: [] };
const activationRequests = [];
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    if (request.url().includes('/api/v1/tools')) result.requests.push({ method: request.method(), url: request.url(), body: request.postData() });
    if (request.method() === 'POST' && /\/api\/gateway\/workspaces\/[^/]+\/activate$/.test(new URL(request.url()).pathname)) activationRequests.push(request.url());
  });
  const api = (url, body) => page.evaluate(async ({ url, body, owner }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const headers = { 'X-Local-Token': credential.token, 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId };
    if (body) headers['Content-Type'] = 'application/json';
    const response = await fetch(url, { method: body ? 'PATCH' : 'GET', headers, body: body ? JSON.stringify(body) : undefined });
    const envelope = await response.json();
    if (!response.ok || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return { status: response.status, requestId: envelope.request_id, data: envelope.data };
  }, { url, body, owner: fixture });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) await api(url);
  result.phase = 'no-session-workspace-tools';
  const firstOpenStarted = performance.now();
  const firstCatalog = page.waitForResponse(response => response.request().method() === 'GET'
    && new URL(response.url()).pathname === '/api/v1/tools'
    && response.request().headers()['x-boxteam-workspace-id'] === fixture.workspaceId);
  await command('boxteam.tools.focus');
  const panel = page.locator('[data-panel-kind="tools"]');
  const content = panel.locator('.native-workspace-panel-content:not([hidden])');
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-workspace-id', fixture.workspaceId);
  const workspaceSelect = panel.getByLabel('工具管理工作区', { exact: true });
  await expect(workspaceSelect).toHaveValue(fixture.workspaceId);
  const firstCatalogResponse = await firstCatalog;
  assert.equal(firstCatalogResponse.status(), 200);
  const firstCatalogBody = await firstCatalogResponse.json();
  assert.equal(firstCatalogBody.request_id, firstCatalogResponse.headers()['x-request-id']);
  await expect(content.getByLabel('Agent', { exact: true })).toBeEnabled();
  await expect(content.locator('.composer-tool-item').first()).toContainText('参数 schema');
  await expect(content.locator('.composer-tool-menu-header')).not.toContainText('加载');
  result.uiAvailability = { firstOpenMs: performance.now() - firstOpenStarted,
    gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId,
    firstRequestId: firstCatalogBody.request_id, toolCount: firstCatalogBody.data.length };
  await workspaceSelect.selectOption(fixture.activeWorkspaceId);
  await expect(panel).toHaveAttribute('data-workspace-id', fixture.activeWorkspaceId);
  await expect(content.getByLabel('Agent', { exact: true })).toBeVisible();
  await workspaceSelect.selectOption(fixture.workspaceId);
  await expect(panel).toHaveAttribute('data-workspace-id', fixture.workspaceId);
  await expect(content.getByLabel('Agent', { exact: true })).toBeVisible();
  const owner = { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId };
  const toolOwner = await api(`/api/v1/workspace`);
  assert.equal(toolOwner.data.workspace_id, fixture.workspaceId);
  const agentId = await content.getByLabel('Agent', { exact: true }).inputValue();
  assert(agentId);
  const catalog = await api(`/api/v1/tools?agent_id=${encodeURIComponent(agentId)}`);
  result.owner = { ...owner, agentId, requestId: catalog.requestId };
  const source = await api('/api/v1/config/sources/workspace_root_mutable_override');
  await command('boxteam.settings.agents', {
    version: 2, gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
    source_key: 'workspace_root_mutable_override', name: source.data.workspace_name ?? fixture.workspaceId,
  }, agentId);
  const manageAgentTools = page.getByRole('button', { name: '管理此 Agent 的工具', exact: true });
  await expect(manageAgentTools).toBeVisible();
  await manageAgentTools.click();
  await expect(content.locator(`[data-agent-id="${agentId}"]`)).toBeVisible();
  result.agentDefinitionReveal = { sourceKey: source.data.source_key, agentId };
  await expect(content.locator('.composer-tool-menu')).toBeVisible();
  const rows = content.locator('.composer-tool-item');
  await expect(rows.first()).toBeVisible();
  await expect(rows.first()).toContainText('参数 schema');

  const testButton = rows.getByRole('button', { name: '测试', exact: true }).first();
  if (await testButton.isEnabled()) {
    await testButton.click();
    const dialog = panel.getByRole('dialog');
    await expect(dialog).toContainText('可能产生真实费用');
    await expect(dialog.getByRole('button', { name: '开始测试', exact: true })).toBeDisabled();
    const providers = dialog.locator('input[type="checkbox"]');
    assert((await providers.count()) >= 2, '所选 Agent 必须提供至少一个可用 Provider');
    await providers.first().check();
    await expect(dialog.getByRole('button', { name: '开始测试', exact: true })).toBeDisabled();
    await dialog.getByLabel('我了解这次测试可能产生模型费用', { exact: true }).check();
    await expect(dialog.getByRole('button', { name: '开始测试', exact: true })).toBeDisabled();
    await dialog.getByLabel('运行次数', { exact: true }).fill('2');
    await expect(dialog.getByRole('button', { name: '开始测试', exact: true })).toBeEnabled();
    await dialog.getByRole('button', { name: '关闭工具面板', exact: true }).click();
  }
  assert.equal(result.requests.some(request => request.method === 'POST' && /\/tests(?:$|\?)/.test(request.url)), false, '未确认的模型测试不能产生POST');

  const mutable = catalog.data.find(tool => !tool.execution_locked && !tool.model_visibility_locked);
  if (mutable) {
    const row = rows.filter({ hasText: mutable.name }).first();
    const toggle = async (capability, currentValue, value) => {
      const label = capability === 'execution'
        ? mutable.name + '：' + (currentValue ? '关闭工具能力' : '开启工具能力')
        : mutable.name + '：' + (currentValue ? '隐藏工具说明' : '显示工具说明');
      if (currentValue === value) return;
      const request = page.waitForResponse(response => response.request().method() === 'PATCH' && new URL(response.url()).pathname === '/api/v1/tools/selection');
      await row.getByRole('button', { name: label, exact: true }).click();
      const response = await request;
      assert.equal(response.status(), 200, await response.text());
      const envelope = await response.json();
      assert.equal(envelope.request_id, response.headers()['x-request-id']);
      const requestHeaders = response.request().headers();
      assert.equal(requestHeaders['x-boxteam-target-gateway-id'], fixture.gatewayId);
      assert.equal(requestHeaders['x-boxteam-workspace-id'], fixture.workspaceId);
      const body = response.request().postDataJSON();
      assert.equal(body.agent_id, agentId);
      assert.deepEqual(body.changes.map(change => change.tool_id), [mutable.tool_id]);
      assert.equal(envelope.data.length, 1);
      assert.equal(envelope.data[0].tool_id, mutable.tool_id);
      result.changes.push({ requestId: envelope.request_id, body, changed: envelope.data });
    };
    await toggle('execution', mutable.execution_enabled, !mutable.execution_enabled);
    const interim = await api('/api/v1/tools?agent_id=' + encodeURIComponent(agentId));
    const current = interim.data.find(tool => tool.tool_id === mutable.tool_id);
    await toggle('execution', current.execution_enabled, mutable.execution_enabled);
    const afterExec = await api('/api/v1/tools?agent_id=' + encodeURIComponent(agentId));
    const after = afterExec.data.find(tool => tool.tool_id === mutable.tool_id);
    await toggle('model', after.model_visible, mutable.model_visible);
    const restored = await api(`/api/v1/tools?agent_id=${encodeURIComponent(agentId)}`);
    const finalTool = restored.data.find(tool => tool.tool_id === mutable.tool_id);
    assert.equal(finalTool.execution_enabled, mutable.execution_enabled);
    assert.equal(finalTool.model_visible, mutable.model_visible);
  }
  assert.equal(result.pageErrors.length, 0, JSON.stringify(result.pageErrors));
  // 关闭再打开同一管理页，计时到正确 owner 的真实目录回读及控件可用。
  await command('workbench.action.closePanel');
  await expect(panel).toBeHidden();
  const warmOpenStarted = performance.now();
  const warmCatalog = page.waitForResponse(response => response.request().method() === 'GET'
    && new URL(response.url()).pathname === '/api/v1/tools'
    && response.request().headers()['x-boxteam-workspace-id'] === fixture.workspaceId);
  await command('boxteam.tools.focus');
  const warmCatalogResponse = await warmCatalog;
  assert.equal(warmCatalogResponse.status(), 200);
  const warmCatalogBody = await warmCatalogResponse.json();
  assert.equal(warmCatalogBody.request_id, warmCatalogResponse.headers()['x-request-id']);
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-workspace-id', fixture.workspaceId);
  await expect(content.getByLabel('Agent', { exact: true })).toHaveValue(agentId);
  await expect(content.getByLabel('Agent', { exact: true })).toBeEnabled();
  await expect(rows.first()).toContainText('参数 schema');
  await expect(content.locator('.composer-tool-menu-header')).not.toContainText('加载');
  result.uiAvailability = { ...result.uiAvailability, warmOpenMs: performance.now() - warmOpenStarted,
    agentId, warmRequestId: warmCatalogBody.request_id, warmToolCount: warmCatalogBody.data.length };
  assert.equal(warmCatalogBody.data.length, catalog.data.length);
  assert(result.uiAvailability.firstOpenMs <= 3000, `工具管理首开 ${result.uiAvailability.firstOpenMs}ms`);
  assert(result.uiAvailability.warmOpenMs <= 3000, `工具管理暖开 ${result.uiAvailability.warmOpenMs}ms`);
  const directory = await api('/api/gateway/workspaces');
  assert.equal(directory.data.active_workspace.workspace_id, fixture.activeWorkspaceId);
  assert.deepEqual(activationRequests, []);
  result.managementTarget = { firstWorkspaceId: fixture.workspaceId, activeWorkspaceId: fixture.activeWorkspaceId,
    selectedWorkspaceId: await workspaceSelect.inputValue(), activationRequests };
  await page.screenshot({ path: path.join(artifacts, 'workspace-tools.png') });
  result.phase = 'completed';
} catch (error) {
  result.error = String(error?.stack ?? error); process.exitCode = 1;
  if (page) { await page.screenshot({ path: path.join(artifacts, 'failure.png') }); result.body = await page.locator('body').innerText(); }
} finally {
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  await browser?.close();
}
