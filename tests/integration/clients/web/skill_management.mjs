import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const baseUrl = process.env.BOXTEAM_SKILL_MANAGEMENT_BASE_URL;
const resultPath = process.env.BOXTEAM_SKILL_MANAGEMENT_RESULT;
const gatewayId = process.env.BOXTEAM_SKILL_MANAGEMENT_GATEWAY_ID;
const remoteGatewayId = process.env.BOXTEAM_SKILL_MANAGEMENT_REMOTE_GATEWAY_ID;
const workspaceId = process.env.BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_ID;
const workspaceName = process.env.BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_NAME;
const remoteSkillName = process.env.BOXTEAM_SKILL_MANAGEMENT_REMOTE_SKILL_NAME;
const remoteSkillMarker = process.env.BOXTEAM_SKILL_MANAGEMENT_REMOTE_SKILL_MARKER;
const localWorkspaceCount = Number(process.env.BOXTEAM_SKILL_MANAGEMENT_LOCAL_WORKSPACE_COUNT);
assert(baseUrl && resultPath && gatewayId && remoteGatewayId && workspaceId && workspaceName
  && remoteSkillName && remoteSkillMarker && localWorkspaceCount > 0);
const result = { phase: 'start', requests: [], errors: [], initialization: [], remoteBrowse: null, remoteRefreshAttempt: null };
let browser, page;
const requestRecords = new WeakMap();
const responseTasks = [];
const save = async () => writeFile(resultPath, JSON.stringify(result, null, 2));
const isObservedPath = pathname => pathname === '/api/gateway/health' || pathname === '/api/gateway/workspaces'
  || pathname === '/api/v1/workspace' || pathname.includes('/skill-sources') || pathname.includes('/config/sources')
  || pathname.includes('/skill-source-connections/')
  || pathname === '/api/v1/resources/gateway-skills' || pathname === '/api/v1/resources/navigation'
  || pathname === '/api/gateway/resource-addresses/parse';
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage();
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.errors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (isObservedPath(url.pathname)) {
      const headers = request.headers(), body = request.postData();
      const observed = { path: url.pathname, method: request.method(), headers: {
        'x-boxteam-target-gateway-id': headers['x-boxteam-target-gateway-id'],
        'x-boxteam-workspace-id': headers['x-boxteam-workspace-id'],
      }, ...(body ? { body: JSON.parse(body) } : {}) };
      result.requests.push(observed);
      requestRecords.set(request, observed);
    }
  });
  page.on('response', response => {
    const observed = requestRecords.get(response.request());
    if (!observed) return;
    const task = (async () => {
      observed.status = response.status();
      observed.requestId = response.headers()['x-request-id'];
      const envelope = await response.json();
      observed.envelopeRequestId = envelope.request_id;
      if (observed.path === '/api/gateway/health' || observed.path === '/api/v1/workspace') {
        observed.data = envelope.data;
        result.initialization.push(observed);
      } else if (observed.path === '/api/gateway/workspaces') {
        const data = envelope.data;
        observed.data = { active_workspace: data.active_workspace, items: data.items.map(item => ({
          gateway_id: item.gateway_id, workspace_id: item.workspace_id, name: item.name, status: item.status,
        })) };
        result.initialization.push(observed);
      } else if (observed.path === '/api/v1/resources/gateway-skills') {
        const data = envelope.data;
        observed.data = { gateway_id: data.gateway_id, workspace_id: data.workspace_id,
          entries: data.entries.map(item => ({ name: item.name, display_uri: item.display_uri,
            available: item.available, error_code: item.error_code })) };
      }
    })().catch(error => { result.errors.push(String(error?.stack ?? error)); });
    responseTasks.push(task);
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.evaluate(async ({ gatewayId, workspaceId }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch('/api/v1/workspace', { headers: {
      'X-Local-Token': credential.token,
      'X-BoxTeam-Target-Gateway-Id': gatewayId,
      'X-BoxTeam-Workspace-Id': workspaceId,
    } });
    if (!response.ok) throw new Error(await response.text());
    await response.json();
  }, { gatewayId, workspaceId });
  await expect.poll(() => result.initialization.filter(item => item.requestId && item.requestId === item.envelopeRequestId).length)
    .toBeGreaterThanOrEqual(3);
  const health = result.initialization.find(item => item.path === '/api/gateway/health');
  const registry = result.initialization.find(item => item.path === '/api/gateway/workspaces');
  const activeWorkspace = result.initialization.find(item => item.path === '/api/v1/workspace');
  assert(health && registry && activeWorkspace);
  assert.equal(health.data.gateway_id, gatewayId);
  assert.deepEqual(registry.data.active_workspace, { gateway_id: gatewayId, workspace_id: workspaceId });
  assert.equal(activeWorkspace.data.workspace_id, workspaceId);
  assert(registry.data.items.some(item => item.gateway_id === remoteGatewayId && item.workspace_id === workspaceId));
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

  result.phase = 'remote-source-browse'; await save();
  running = execute('boxteam.quickAccess.resources', { gatewayId: remoteGatewayId, workspaceId })
    .catch(error => { result.errors.push(String(error)); });
  await expect(quick).toBeVisible();
  const remotePick = quick.locator('.monaco-list-row').filter({ hasText: remoteSkillName }).first();
  await expect(remotePick).toBeVisible({ timeout: 45000 });
  result.remoteBrowse = { pickerRow: await remotePick.innerText(), owner: { gateway_id: remoteGatewayId, workspace_id: workspaceId } };
  await remotePick.click();
  await running;
  await expect(quick).not.toBeVisible();
  const remoteBody = page.locator('.editor-group-container .monaco-editor .view-lines').filter({ visible: true });
  await expect(remoteBody).toContainText(remoteSkillMarker, { timeout: 30000 });

  result.phase = 'remote-refresh-unavailable'; await save();
  const refreshCounts = () => ({
    gateway: result.requests.filter(item => item.method === 'POST' && item.path === '/api/gateway/skill-sources/refresh').length,
    workspace: result.requests.filter(item => item.method === 'POST' && item.path === '/api/v1/resources/skill-sources/refresh').length,
    remote: result.requests.filter(item => item.method === 'POST' && item.path.endsWith('/refresh') && (
      item.headers['x-boxteam-target-gateway-id'] === remoteGatewayId
      || item.path.startsWith('/api/gateway/skill-source-connections/')
    )).length,
  });
  const beforeRemoteAttempt = refreshCounts();
  running = execute('boxteam.skills.refreshWorkspaceSources').catch(error => { result.errors.push(String(error)); });
  await expect(quick).toBeVisible();
  const refreshTargets = await quick.locator('.monaco-list-row').allInnerTexts();
  assert.equal(refreshTargets.length, localWorkspaceCount, JSON.stringify(refreshTargets));
  result.remoteRefreshAttempt = { available: false, offeredTargets: refreshTargets, before: beforeRemoteAttempt };
  await quick.press('Escape');
  await running;
  await expect(quick).not.toBeVisible();
  result.remoteRefreshAttempt.after = refreshCounts();
  assert.deepEqual(result.remoteRefreshAttempt.after, beforeRemoteAttempt);

  result.phase = 'gateway-refresh'; await save();
  await execute('boxteam.skills.refreshGatewaySources');
  await expect(page.getByText(/本机 Gateway Skill 来源已刷新|Local Gateway Skill sources refreshed/).last()).toBeVisible();
  result.phase = 'workspace-refresh'; await save();
  running = execute('boxteam.skills.refreshWorkspaceSources').catch(error => { result.errors.push(String(error)); });
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: workspaceName }).first().click();
  await running;
  await expect(page.getByText(/工作区 Skill 来源已刷新|Workspace Skill sources refreshed/).last()).toBeVisible();
  await Promise.all(responseTasks);
  result.phase = 'completed';
} catch (cause) {
  result.errors.push(String(cause?.stack ?? cause));
  if (page) result.body = await page.locator('body').innerText().catch(() => '');
  process.exitCode = 1;
} finally {
  await save();
  await browser?.close();
}
