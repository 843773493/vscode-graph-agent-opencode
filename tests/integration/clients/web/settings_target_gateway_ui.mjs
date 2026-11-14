import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const input = JSON.parse(process.env.BOXTEAM_SETTINGS_TARGET_UI_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], externalRequests: [], sourceReads: [] };
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== new URL(input.baseUrl).origin) result.externalRequests.push(request.url());
    if (request.method() === 'GET' && url.pathname.startsWith('/api/v1/config/sources')) {
      const headers = request.headers();
      result.sourceReads.push({ path: url.pathname, gateway_id: headers['x-boxteam-target-gateway-id'],
        workspace_id: headers['x-boxteam-workspace-id'] });
    }
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench')).toBeVisible();
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand, undefined, { timeout: 30_000 });
  const credential = await page.evaluate(async () => (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token);
  const active = await page.evaluate(async token => {
    const response = await fetch('/api/gateway/workspaces', { headers: { 'X-Local-Token': token } });
    const body = await response.json();
    if (!response.ok || response.headers.get('X-Request-ID') !== body.request_id) throw new Error(JSON.stringify(body));
    return body.data.active_workspace;
  }, credential);
  assert.equal(active.gateway_id, input.gatewayA);
  assert.equal(active.workspace_id, input.workspaceId);
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([input.gatewayA, input.workspaceId])}`)).toBeVisible();

  const quick = page.locator('.quick-input-widget');
  const chooseSource = async () => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: 'workspace_root_mutable_override' }).first().click();
  };
  const openAgentManagement = async () => {
    const opening = page.evaluate(() => globalThis.workbench.commands.executeCommand('boxteam.settings.agents'));
    await chooseSource();
    await opening;
    const form = page.locator('.editor-group-container.active .configuration-entities[data-definition-kind="agents"]');
    await expect(form).toBeVisible();
    return page.locator('.editor-group-container.active .settings-context-label');
  };
  const header = await openAgentManagement();
  const gatewaySelector = header.getByRole('combobox', { name: '设置所属 Gateway', exact: true });
  await expect(gatewaySelector).toBeEnabled();
  await expect(header).toContainText(input.gatewayA);
  await expect(header).toContainText(input.workspaceId);
  const localRead = result.sourceReads.find(read => read.gateway_id === input.gatewayA && read.workspace_id === input.workspaceId);
  assert(localRead, '首次 Agent 管理必须读取本地 Gateway 的首个工作区');

  result.phase = 'explicit-remote';
  const readsBeforeRemote = result.sourceReads.length;
  await gatewaySelector.selectOption({ label: `${input.remoteName} · ${input.gatewayB}` });
  await expect(header).toContainText(input.gatewayB);
  await expect(header).toContainText(input.workspaceId);
  await expect.poll(() => result.sourceReads.slice(readsBeforeRemote).some(read =>
    read.gateway_id === input.gatewayB && read.workspace_id === input.workspaceId)).toBe(true);
  const remoteRead = result.sourceReads.slice(readsBeforeRemote).find(read =>
    read.gateway_id === input.gatewayB && read.workspace_id === input.workspaceId);
  assert(remoteRead, '显式选择后 Settings 必须真实读取 Gateway B 的 Workspace 来源');

  result.phase = 'switch-back-local';
  const readsBeforeLocal = result.sourceReads.length;
  await gatewaySelector.selectOption({ label: `本地 · ${input.gatewayA}` });
  await expect(header).toContainText(input.gatewayA);
  await expect(header).toContainText(input.workspaceId);
  await expect.poll(() => result.sourceReads.slice(readsBeforeLocal).some(read =>
    read.gateway_id === input.gatewayA && read.workspace_id === input.workspaceId)).toBe(true);
  const returnedLocalRead = result.sourceReads.slice(readsBeforeLocal).find(read =>
    read.gateway_id === input.gatewayA && read.workspace_id === input.workspaceId);
  assert(returnedLocalRead, '切回本地后设置来源读取必须回到 Gateway A');

  result.phase = 'remembered-global-agent-entry';
  const readsBeforeEntry = result.sourceReads.length;
  await openAgentManagement();
  await expect.poll(() => result.sourceReads.slice(readsBeforeEntry).some(read =>
    read.gateway_id === input.gatewayA && read.workspace_id === input.workspaceId)).toBe(true);
  assert(!result.sourceReads.slice(readsBeforeEntry).some(read => read.gateway_id === input.gatewayB),
    '重新打开 Agent 管理入口不能恢复远端 Gateway 目标');
  const finalActive = await page.evaluate(async token => {
    const response = await fetch('/api/gateway/workspaces', { headers: { 'X-Local-Token': token } });
    const body = await response.json();
    if (!response.ok || response.headers.get('X-Request-ID') !== body.request_id) throw new Error(JSON.stringify(body));
    return body.data.active_workspace;
  }, credential);
  assert.deepEqual(finalActive, active, '设置目标切换不能激活或创建工作区');
  result.settingsTargetRoundTrip = { startupDefault: input.gatewayA, selectedRemote: remoteRead.gateway_id,
    switchedBack: returnedLocalRead.gateway_id, rememberedAgentEntry: input.gatewayA, activeWorkspaceUnchanged: true,
    sourceReads: result.sourceReads };
  result.phase = 'complete';
} catch (error) {
  result.error = error instanceof Error ? error.stack : String(error);
  throw error;
} finally {
  if (page && result.phase !== 'complete') {
    try { result.screenshotPath = `${artifacts}/settings-target-gateway-failure.png`;
      await page.screenshot({ path: result.screenshotPath, fullPage: true }); }
    catch (error) { result.screenshotError = error instanceof Error ? error.message : String(error); }
  }
  if (browser) await browser.close();
  await writeFile(`${artifacts}/settings-target-gateway-result.json`, `${JSON.stringify(result, null, 2)}\n`);
}
