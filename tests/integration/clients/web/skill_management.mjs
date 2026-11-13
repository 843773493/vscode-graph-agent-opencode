import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const baseUrl = process.env.BOXTEAM_SKILL_MANAGEMENT_BASE_URL;
const resultPath = process.env.BOXTEAM_SKILL_MANAGEMENT_RESULT;
const gatewayId = process.env.BOXTEAM_SKILL_MANAGEMENT_GATEWAY_ID;
const workspaceId = process.env.BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_ID;
const workspaceName = process.env.BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_NAME;
assert(baseUrl && resultPath && gatewayId && workspaceId && workspaceName);
const result = { phase: 'start', requests: [], errors: [] };
let browser, page;
const save = async () => writeFile(resultPath, JSON.stringify(result, null, 2));
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage();
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.errors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.includes('/skill-sources') || url.pathname.includes('/config/sources')) {
      const headers = request.headers();
      result.requests.push({ path: url.pathname, method: request.method(), headers: {
        'x-boxteam-target-gateway-id': headers['x-boxteam-target-gateway-id'],
        'x-boxteam-workspace-id': headers['x-boxteam-workspace-id'],
      } });
    }
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  const execute = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const quick = page.locator('.quick-input-widget');
  result.phase = 'gateway-source-open'; await save();
  let running = execute('boxteam.skills.openGatewaySources').catch(error => { result.errors.push(String(error)); });
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').first().click();
  await running;
  await expect(quick).not.toBeVisible();
  result.gatewaySourceEditor = await page.locator('.tabs-container .tab.active').innerText();
  result.phase = 'workspace-source-open'; await save();
  running = execute('boxteam.skills.openWorkspaceSources').catch(error => { result.errors.push(String(error)); });
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: workspaceName }).first().click();
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').first().click();
  await running;
  await expect(quick).not.toBeVisible();
  result.workspaceSourceEditor = await page.locator('.tabs-container .tab.active').innerText();
  result.phase = 'gateway-refresh'; await save();
  await execute('boxteam.skills.refreshGatewaySources');
  await expect(page.getByText(/本机 Gateway Skill 来源已刷新|Local Gateway Skill sources refreshed/).last()).toBeVisible();
  result.phase = 'workspace-refresh'; await save();
  running = execute('boxteam.skills.refreshWorkspaceSources').catch(error => { result.errors.push(String(error)); });
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: workspaceName }).first().click();
  await running;
  await expect(page.getByText(/工作区 Skill 来源已刷新|Workspace Skill sources refreshed/).last()).toBeVisible();
  result.phase = 'completed';
} catch (cause) {
  result.errors.push(String(cause?.stack ?? cause));
  if (page) result.body = await page.locator('body').innerText().catch(() => '');
  process.exitCode = 1;
} finally {
  await save();
  await browser?.close();
}
