import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { fileSearchRequests, searchReceipt } from '../../../support/workbench/search-observation.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshotPath = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!baseUrl || !resultPath || !screenshotPath) throw new Error('工作区归属恢复验收缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/test_workspace_owner_recovery.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], switches: [], restored: [], searches: [], breadcrumbs: [],
  boundary: '正式 Workbench、两个真实 Gateway／Backend；仅复用现有 SSH 本机执行及 loopback 隧道装具' };
let page, failure;
const responseReads = [];

async function api(path, init = {}) {
  return page.evaluate(async ({ path, init }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return body;
  }, { path, init });
}
const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
const open = uri => command('_workbench.open', uri, [undefined, { pinned: true }]);
const key = owner => JSON.stringify([owner.gatewayId, owner.workspaceId]);
const threadUri = owner => `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${owner.sessionId}/${owner.threadId}`;
const thread = owner => page.locator(`section.boxteam-thread[data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
const editor = () => page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();

async function switchTo(owner) {
  const row = page.getByTestId(`workspace-node-${key(owner)}`);
  await row.locator('button.workspace-label').click();
  await expect(row).toHaveAttribute('aria-current', 'location');
  expect((await api('/api/gateway/workspaces')).data.active_workspace).toEqual({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId });
  result.switches.push(owner.gatewayId);
}

async function searchOwner(owner) {
  const start = result.searches.length;
  const receiptStart = responseReads.length;
  await command('workbench.action.quickOpen', '%identityowner');
  const quick = page.locator('.quick-input-widget');
  await expect(quick).toContainText(`identityowner ${owner.label} 正文`);
  await expect(quick).not.toContainText(`identityowner ${owner.label === 'A' ? 'B' : 'A'} 正文`);
  await expect.poll(() => result.searches.slice(start).length).toBeGreaterThan(0);
  expect(result.searches.slice(start).every(request => request.gatewayId === owner.gatewayId && request.workspaceId === owner.workspaceId)).toBe(true);
  // 命中先于 SSE 结算显示；等原协议 end 再关闭 Picker，避免验收自己取消取证响应。
  await expect.poll(() => responseReads.length).toBeGreaterThan(receiptStart);
  await Promise.all(responseReads.slice(receiptStart));
  await page.keyboard.press('Escape');
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    result.searches.push(...fileSearchRequests(request));
    const match = new URL(request.url()).pathname.match(/^\/api\/v1\/session-catalog\/breadcrumb\/([^/]+)$/);
    if (match) result.breadcrumbs.push({ sessionId: match[1], gatewayId: request.headers()['x-boxteam-target-gateway-id'],
      workspaceId: request.headers()['x-boxteam-workspace-id'], phase: result.phase });
  });
  page.on('response', response => {
    const request = response.request();
    if (new URL(request.url()).pathname === '/api/gateway/search') responseReads.push(searchReceipt(request));
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  const targets = [...listing.data.items].sort((a, b) => Number(b.gateway_id === health.data.gateway_id) - Number(a.gateway_id === health.data.gateway_id));
  expect(targets).toHaveLength(2);
  expect(targets[0].workspace_id).toBe(targets[1].workspace_id);
  expect(targets[0].gateway_id).not.toBe(targets[1].gateway_id);
  const owners = [];
  for (const [index, target] of targets.entries()) {
    const headers = { 'X-BoxTeam-Workspace-Id': target.workspace_id, 'X-BoxTeam-Target-Gateway-Id': target.gateway_id };
    const workspace = await api('/api/v1/workspace', { headers });
    expect(workspace.data.workspace_id).toBe(target.workspace_id);
    const roots = await api('/api/v1/workspace/files/roots', { headers });
    const session = (await api('/api/v1/sessions', { method: 'POST', headers,
      body: JSON.stringify({ title: `身份恢复 ${index === 0 ? 'A' : 'B'}`, agent_id: 'default' }) })).data;
    const main = (await api(`/api/v1/sessions/${session.session_id}/main-thread`, { headers })).data;
    const owner = { gatewayId: target.gateway_id, workspaceId: target.workspace_id, rootUri: roots.data.workspace_uri,
      sessionId: session.session_id, threadId: main.thread_id, label: index === 0 ? 'A' : 'B' };
    owner.fileUri = await page.evaluate(owner => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: owner.gatewayId,
      path: `/${owner.workspaceId}/${encodeURIComponent(owner.rootUri)}/identity-owner.txt` }).toString(), owner);
    owners.push(owner);
  }
  result.owners = owners;
  result.phase = 'switch-file-search-and-draft';
  for (const owner of owners) {
    await switchTo(owner);
    await open(owner.fileUri);
    await expect(editor().locator('.view-lines')).toContainText(`identityowner ${owner.label} 正文`);
    await searchOwner(owner);
    await open(threadUri(owner));
    await thread(owner).getByTestId('composer-input').fill(`仅 ${owner.label} 草稿`);
  }
  // 活动目标改变后重开 A，再回到 B；既有标签及草稿不随当前选择改投。
  for (const owner of owners) {
    await switchTo(owner);
    await open(threadUri(owner));
    await expect(thread(owner).getByTestId('composer-input')).toHaveValue(`仅 ${owner.label} 草稿`);
  }
  await command('boxteam.flushViewState');
  result.phase = 'reload-original-owners';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId(`workspace-node-${key(owners[1])}`)).toHaveAttribute('aria-current', 'location');
  await expect(page.locator('.tabs-container .tab[data-resource-name="identity-owner.txt"]')).toHaveCount(2);
  for (const owner of owners) {
    await expect(page.locator(`.tabs-container .tab[data-resource-name="${owner.threadId}"]`)).toHaveCount(1);
    // 必须先证明原版恢复已建立标签，再打开同 URI 激活；不能以重新创建代替恢复。
    await open(threadUri(owner));
    await expect(thread(owner).getByTestId('composer-input')).toHaveValue(`仅 ${owner.label} 草稿`);
    await open(owner.fileUri);
    await expect(editor().locator('.view-lines')).toContainText(`identityowner ${owner.label} 正文`);
    await searchOwner(owner);
    result.restored.push(owner.gatewayId);
  }
  const receipts = await Promise.all(responseReads);
  expect(receipts.length).toBeGreaterThanOrEqual(4);
  for (const receipt of receipts) {
    expect(receipt.status).toBe(200);
    expect(receipt.scope.owners).toHaveLength(1);
    const owner = owners.find(value => value.gatewayId === receipt.scope.owners[0].gateway_id);
    expect(owner).toBeDefined();
    expect(receipt.scope.owners[0].workspace_id).toBe(owner.workspaceId);
    for (const page of receipt.pages) {
      expect(page.branch.branch.owner).toEqual({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId });
    }
  }
  result.receipts = receipts;
  for (const request of result.breadcrumbs) {
    const owner = owners.find(value => value.sessionId === request.sessionId);
    expect(owner).toBeDefined();
    expect(request.gatewayId).toBe(owner.gatewayId);
    expect(request.workspaceId).toBe(owner.workspaceId);
  }
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) { failure = error; result.failure = String(error.stack ?? error); }
finally {
  const errors = failure ? [failure] : [];
  await Promise.allSettled(responseReads);
  if (failure && page) { try { await page.screenshot({ path: screenshotPath, fullPage: true }); } catch (error) { errors.push(error); } }
  try { await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n'); } catch (error) { errors.push(error); }
  try { await run.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, '工作区归属恢复验收失败');
}
