import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshotPath = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!baseUrl || !resultPath || !screenshotPath) throw new Error('Thread 身份验收缺少运行参数');
const scope = TestRunContext.fromTestFile('tests/integration/clients/web/test_thread_editor_identity.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], initialization: [],
  boundary: '真实 Gateway、完整工作台与托管后端；仅歧义场景将登记列表复制一条，其余响应保持真实' };
let page;
let failure;

async function api(path, init = {}) {
  return page.evaluate(async ({ path, init }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ status: response.status, body }));
    return body;
  }, { path, init });
}
async function command(id, ...args) {
  return page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
}
async function open(uri) { await command('_workbench.open', uri, [undefined, { pinned: true }]); }

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  scope.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext();
  scope.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  result.phase = 'create-through-product-api';
  const health = await api('/api/gateway/health');
  const workspaces = await api('/api/gateway/workspaces');
  const routeId = workspaces.data.active_workspace_id;
  const headers = { 'X-BoxTeam-Workspace-Id': routeId };
  const workspace = await api('/api/v1/workspace', { headers });
  result.initialization = [health, workspaces, workspace].map(body => ({ requestId: body.request_id }));
  const session = (await api('/api/v1/sessions', { method: 'POST', headers,
    body: JSON.stringify({ title: '稳定身份恢复验收', agent_id: 'default' }) })).data;
  const main = (await api(`/api/v1/sessions/${session.session_id}/main-thread`, { headers })).data;
  const owner = { gatewayId: health.data.gateway_id, workspaceId: session.workspace_id,
    sessionId: session.session_id, threadId: main.thread_id, routeId };
  result.owner = owner;
  const uri = `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${owner.sessionId}/${owner.threadId}`;
  const threadSelector = `section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`;
  const tabs = page.locator(`.tabs-container .tab[data-resource-name="${owner.threadId}"]`);
  const thread = page.locator(threadSelector);
  result.phase = 'open-and-deduplicate';
  await open(uri);
  await expect(thread.getByTestId('composer-input')).toBeVisible();
  await open(uri);
  await expect(tabs).toHaveCount(1);
  // 同一资源再由真实导航打开，不能因入口或标题不同创建另一标签。
  await page.getByRole('button', { name: '刷新资源树', exact: true }).click();
  await page.locator(`button[data-session-id="${owner.sessionId}"]`).dblclick();
  await expect(tabs).toHaveCount(1);
  result.stableOpenDeduplicated = true;

  result.phase = 'split-close-reopen-reload';
  await thread.getByTestId('composer-input').fill('刷新后保留的草稿');
  await thread.getByRole('button', { name: '切换会话资源', exact: true }).click();
  await command('workbench.action.splitEditorRight');
  await expect(tabs).toHaveCount(2);
  const groups = page.locator('.editor-group-container');
  await groups.nth(1).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true }).click();
  await expect(groups.nth(0).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await command('workbench.action.closeActiveEditor');
  await command('workbench.action.reopenClosedEditor');
  // 原版移除空组；当前组已有同一资源时，重开历史不会制造重复标签。
  await expect(tabs).toHaveCount(1);
  await command('workbench.action.splitEditorRight');
  await expect(tabs).toHaveCount(2);
  await groups.nth(1).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true }).click();
  await command('boxteam.flushViewState');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(tabs).toHaveCount(2);
  for (const group of [groups.nth(0), groups.nth(1)]) {
    await expect(group.locator(threadSelector).getByTestId('composer-input')).toHaveValue('刷新后保留的草稿');
  }
  await expect(groups.nth(0).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(groups.nth(1).locator(threadSelector).getByRole('button', { name: '切换会话资源', exact: true })).toHaveAttribute('aria-expanded', 'false');
  result.splitAndReloadPreserved = true;

  result.phase = 'reject-unknown-gateway';
  const rejectedSession = 'session-must-not-be-requested';
  const reads = [];
  const observe = request => { if (new URL(request.url()).pathname.includes(rejectedSession)) reads.push(request.url()); };
  page.on('request', observe);
  await open(`boxteam-thread://gateway-unknown/${owner.workspaceId}/${rejectedSession}/${owner.threadId}`);
  await expect(page.getByText('目标 Gateway／工作区缺少唯一的已登记路由，无法打开资源', { exact: false }).first()).toBeVisible();
  expect(reads).toEqual([]);
  result.unknownGatewayRejected = true;
  await command('workbench.action.closeActiveEditor');

  result.phase = 'reject-ambiguous-registry';
  const duplicate = async route => {
    const response = await route.fetch();
    const body = await response.json();
    const source = body.data.items.find(item => item.workspace_id === routeId);
    body.data.items.push({ ...source, workspace_id: 'explicit-duplicate-route' });
    await route.fulfill({ response, json: body });
  };
  const registryUrl = url => url.pathname === '/api/gateway/workspaces';
  await page.route(registryUrl, duplicate);
  try {
    await open(`boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${rejectedSession}/${owner.threadId}`);
    await expect(page.getByText('目标 Gateway／工作区缺少唯一的已登记路由，无法打开资源', { exact: false }).first()).toBeVisible();
    expect(reads).toEqual([]);
    result.ambiguousRouteRejected = true;
  } finally {
    // 关闭原版错误编辑器仍可能触发导航重解；先等已接收的替身请求结束再释放页面。
    await page.unrouteAll({ behavior: 'wait' });
    page.off('request', observe);
  }
  await command('workbench.action.closeActiveEditor');
  await expect(page.locator(threadSelector).first().getByTestId('composer-input')).toBeVisible();
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) { failure = error; result.failure = String(error.stack ?? error); }
finally {
  const errors = failure ? [failure] : [];
  if (failure && page) { try { await page.screenshot({ path: screenshotPath, fullPage: true }); } catch (error) { errors.push(error); } }
  try { await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n'); } catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, 'Thread 稳定身份验收失败');
}
