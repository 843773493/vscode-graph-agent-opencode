import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须指定浏览器产物目录');
const result = { phase: 'initialization', pageErrors: [], writes: [], initialization: [] };
let browser;
let page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname.includes('/config/sources/')) result.writes.push(request.postDataJSON());
  });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
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
    await page.evaluate(() => { void globalThis.workbench.commands.executeCommand('workbench.mcp.addConfiguration'); });
    await pick(workspace.name);
    await input(id);
    await expect(page.getByRole('textbox', { name: '选择 MCP 服务来源', exact: true })).toBeVisible();
  };
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
  await pick('boxteam.mcp-stdio@0.1.0');
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
  result.phase = 'resource-quick-access';
  const mcpWrites = [];
  const resourceOwnerReads = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname.includes('/api/v1/mcp/')) mcpWrites.push(request.url());
    if (result.phase === 'resource-quick-access' && request.method() === 'GET'
      && url.pathname.startsWith('/api/v1/resources')) {
      const headers = request.headers();
      resourceOwnerReads.push({ path: url.pathname, gatewayId: headers['x-boxteam-target-gateway-id'],
        workspaceId: headers['x-boxteam-workspace-id'] });
    }
  });
  const findMcp = async () => {
    await page.evaluate(owner => globalThis.workbench.commands.executeCommand('boxteam.quickAccess.resources', owner),
      { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId });
    await quick.locator('input').first().fill('resource ui-plugin');
    const row = quick.locator('.monaco-list-row').filter({ hasText: 'ui-plugin' });
    await expect(row).toHaveCount(1); await expect(row).toContainText('MCP');
    return row;
  };
  const lookup = await findMcp();
  expect(resourceOwnerReads.length).toBeGreaterThan(0);
  expect(resourceOwnerReads.every(request => request.gatewayId === fixture.gatewayId
    && request.workspaceId === fixture.workspaceId)).toBe(true);
  await lookup.click();
  await expect(page.locator('.tab').filter({ hasText: 'MCP Server: ui-plugin' })).toBeVisible();
  const details = page.locator('.mcp-server-editor').filter({ visible: true });
  await expect(details).toContainText('ui-plugin'); await expect(details).toContainText(workspace.name);
  const optionsLookup = await findMcp();
  await optionsLookup.hover();
  await optionsLookup.getByRole('button', { name: '更多 MCP 动作', exact: true }).click();
  await expect(quick).toContainText('Show Details'); await quick.locator('input').first().press('Escape');
  assert.deepEqual(mcpWrites, [], '发现、详情及取消管理菜单不改变MCP连接');
  assert.equal(result.writes.length, 2);
  result.mcpQuickAccess = true;
  assert.deepEqual(result.pageErrors, []);
  result.phase = 'completed';
} finally {
  try {
    if (page) await page.screenshot({ path: path.join(artifacts, 'mcp-add.png'), fullPage: true });
    await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  } finally {
    if (browser) await browser.close();
  }
}
