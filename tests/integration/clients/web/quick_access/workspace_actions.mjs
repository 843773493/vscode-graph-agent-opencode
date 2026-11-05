// Integration：正式 Workbench／真实 Gateway 和 Backend；模型回放仅供启动，不替换响应或业务 writer。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`工作区动作验收缺少 ${name}`);
  return value;
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_workspace_actions.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], discoveries: [], domainSearches: [],
  lifecycleWrites: [], receipts: [], apiReceipts: [], forbiddenWrites: [], uiMessages: [],
  boundary: '正式Workbench、真实Gateway／托管Backend和原React导航writer；B在准备阶段登记并停止；无响应替身或浏览器存储预写；模型回放仅供启动' };
const responseReads = [], responseErrors = [];
const checkpoint = async phase => {
  result.phase = phase;
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
};
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('workspace-actions-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1500, height: 1100 } });
  run.addCleanup('workspace-actions-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  const lifecycle = request => request.method() === 'POST'
    && /\/api\/gateway\/workspaces\/[^/]+\/(activate|reconnect|runtime\/(start|stop))$/.test(new URL(request.url()).pathname);
  page.on('request', request => {
    const url = new URL(request.url()), headers = request.headers();
    const observed = { phase: result.phase, path: url.pathname, method: request.method(),
      owner: { gateway_id: headers['x-boxteam-target-gateway-id'], workspace_id: headers['x-boxteam-workspace-id'] },
      params: Object.fromEntries(url.searchParams) };
    if (url.pathname === '/api/gateway/search/workspaces') result.discoveries.push(observed);
    if (url.pathname === '/api/gateway/search' || /^\/api\/v1\/(session-catalog|session-threads)\/search$/.test(url.pathname)) result.domainSearches.push(observed);
    if (lifecycle(request)) result.lifecycleWrites.push(observed);
    if (request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(url.pathname)) result.forbiddenWrites.push(observed);
  });
  page.on('requestfinished', request => {
    if (!lifecycle(request)) return;
    const phase = result.phase;
    responseReads.push((async () => {
      const response = await request.response(), body = await response.json();
      result.receipts.push({ phase, path: new URL(request.url()).pathname, status: response.status(),
        header: response.headers()['x-request-id'], request_id: body.request_id });
    })().catch(error => responseErrors.push(String(error))));
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = async (path, owner, data) => {
    const receipt = await page.evaluate(async ({ path, owner, data }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
        body: data === undefined ? undefined : JSON.stringify(data), headers: {
          'X-Local-Token': credential.data.token, 'Content-Type': 'application/json',
          ...(owner ? { 'X-BoxTeam-Workspace-Id': owner.workspace_id, 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id } : {}),
        } });
      return { path, status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, owner, data });
    result.apiReceipts.push({ phase: result.phase, ...receipt });
    expect(receipt.status, JSON.stringify(receipt)).toBe(200);
    expect(receipt.body.request_id).toBe(receipt.header);
    return receipt.body.data;
  };
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const owner = ({ gateway_id, workspace_id }) => ({ gateway_id, workspace_id });
  const health = await api('/api/gateway/health'), initial = await api('/api/gateway/workspaces');
  const ownerA = initial.active_workspace, metadata = await api('/api/v1/workspace');
  expect(ownerA.gateway_id).toBe(health.gateway_id);
  expect(metadata.workspace_id).toBe(ownerA.workspace_id);
  await api('/api/gateway/managed-workspaces', undefined,
    { root_path: required('BOXTEAM_ACTION_OTHER_WORKSPACE'), name: 'Workspace Actions B' });
  const registered = await api('/api/gateway/workspaces?check_health=false');
  const b = registered.items.find(item => item.name === 'Workspace Actions B');
  expect(b?.gateway_id).toBe(health.gateway_id);
  expect(b?.workspace_id).toBeTruthy();
  const ownerB = owner(b), key = target => JSON.stringify([target.gateway_id, target.workspace_id]);
  result.owners = { a: ownerA, b: ownerB };
  await api(`/api/gateway/workspaces/${ownerA.workspace_id}/activate`, ownerA, {});
  await api(`/api/gateway/workspaces/${b.workspace_id}/runtime/stop`, ownerB, {});
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const current = () => api('/api/gateway/workspaces?check_health=false');
  const ready = await current();
  expect(ready.active_workspace).toEqual(ownerA);
  expect(ready.items.find(item => key(item) === key(ownerB)).status).toBe('offline');
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const row = quick.locator('.monaco-list-row').filter({ hasText: 'Workspace Actions B' }).filter({ hasText: b.workspace_id });
  const show = async () => {
    await command('workbench.action.quickOpen', 'workspace Workspace Actions B');
    await expect(input).toHaveValue('workspace Workspace Actions B');
    await expect(row).toContainText('offline');
  };
  const activeId = () => quick.locator('.monaco-list-row.focused [data-quick-input-id]').getAttribute('data-quick-input-id');
  const phaseWrites = phase => result.lifecycleWrites.filter(item => item.phase === phase);
  const choose = async label => {
    await page.keyboard.press('Enter');
    await expect(quick).toContainText('工作区动作 · Workspace Actions B');
    await quick.locator('.monaco-list-row').filter({ hasText: label }).click();
  };

  await checkpoint('browse');
  await show();
  await expect.poll(activeId).toBe(key(ownerB));
  await page.keyboard.press('Escape');
  expect(phaseWrites(result.phase)).toEqual([]);
  expect(result.domainSearches.filter(item => item.phase === result.phase)).toEqual([]);
  expect((await current()).active_workspace).toEqual(ownerA);
  result.browseOnly = true;

  await checkpoint('scope-cancel');
  await show();
  await quick.getByRole('button', { name: '选择资源搜索范围', exact: true }).click();
  await expect(quick).toContainText('选择快速访问搜索范围');
  await quick.locator('.monaco-list-row').filter({ hasText: ownerB.gateway_id }).filter({ hasText: ownerB.workspace_id }).getByRole('checkbox').check();
  await page.keyboard.press('Escape');
  await expect(input).toHaveValue('workspace Workspace Actions B');
  await expect.poll(activeId).toBe(key(ownerB));
  expect(phaseWrites(result.phase)).toEqual([]);
  expect((await current()).active_workspace).toEqual(ownerA);
  result.scopeCancelRestored = true;

  await checkpoint('scope-only');
  await choose('在此工作区查找资源');
  await expect(input).toHaveValue('');
  await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
  await expect(quick).toContainText('选择快速访问搜索范围');
  await expect(quick.locator('.monaco-list-row').filter({ hasText: ownerB.gateway_id }).filter({ hasText: ownerB.workspace_id }).getByRole('checkbox')).toBeChecked();
  await expect(quick.locator('.monaco-list-row').filter({ hasText: ownerA.gateway_id }).filter({ hasText: ownerA.workspace_id }).getByRole('checkbox')).not.toBeChecked();
  await page.keyboard.press('Escape');
  await expect(input).toHaveValue('');
  await page.keyboard.press('Escape');
  expect(phaseWrites(result.phase)).toEqual([]);
  expect(result.domainSearches.filter(item => item.phase === result.phase)).toEqual([]);
  expect((await current()).active_workspace).toEqual(ownerA);
  result.scopeOnly = true;

  await checkpoint('explicit-start');
  await show();
  await choose('启动工作区 Backend');
  let confirmationHandled = false;
  await expect.poll(async () => {
    const dialog = page.getByRole('dialog');
    if (!confirmationHandled && await dialog.isVisible()) {
      result.startConfirmation = await dialog.innerText();
      expect(result.startConfirmation).toContain(ownerB.workspace_id);
      await dialog.getByRole('button', { name: '启动', exact: true }).click();
      confirmationHandled = true;
    }
    result.uiMessages = await page.locator('.notification-list-item-message').allTextContents();
    await checkpoint('explicit-start');
    return phaseWrites('explicit-start').length ? 'sent' : result.uiMessages.length ? result.uiMessages.join('\n') : 'pending';
  }, { timeout: 10_000 }).toBe('sent');
  await expect.poll(async () => (await current()).items.find(item => key(item) === key(ownerB)).status, { timeout: 40_000 }).toBe('ready');
  await expect(page.getByTestId(`workspace-node-${key(ownerB)}`).locator('button.workspace-label')).toBeEnabled();
  expect(phaseWrites(result.phase)).toEqual([expect.objectContaining({ path: `/api/gateway/workspaces/${b.workspace_id}/runtime/start`, owner: ownerB })]);
  expect((await current()).active_workspace).toEqual(ownerA);
  result.startedWithoutActivation = true;

  await checkpoint('explicit-activate');
  await command('workbench.action.quickOpen', 'workspace Workspace Actions B');
  await expect(row).toContainText('ready');
  await choose('切换到此工作区');
  await expect.poll(async () => (await current()).active_workspace).toEqual(ownerB);
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeEnabled();
  expect(phaseWrites(result.phase)).toEqual([expect.objectContaining({ path: `/api/gateway/workspaces/${b.workspace_id}/activate`, owner: ownerB })]);
  result.activatedExplicitly = true;

  await checkpoint('original-menu');
  await page.getByTestId(`workspace-node-${key(ownerA)}`).locator('button.workspace-label').click();
  await expect.poll(async () => (await current()).active_workspace).toEqual(ownerA);
  expect(phaseWrites(result.phase)).toEqual([expect.objectContaining({ path: `/api/gateway/workspaces/${ownerA.workspace_id}/activate`, owner: ownerA })]);
  expect((await api('/api/v1/workspace')).workspace_id).toBe(ownerA.workspace_id);
  result.originalMenu = true;
  await Promise.all(responseReads);
  expect(responseErrors).toEqual([]);
  for (const receipt of result.receipts) {
    expect(receipt.status).toBe(200); expect(receipt.request_id).toBeTruthy(); expect(receipt.request_id).toBe(receipt.header);
  }
  expect(result.discoveries.some(item => item.params.query === 'Workspace Actions B')).toBe(true);
  expect(result.discoveries.every(item => item.method === 'GET')).toBe(true);
  expect(result.forbiddenWrites).toEqual([]);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) { failure = error; }
finally {
  try { if (failure && page) await page.screenshot({ path: output.replace(/\.json$/, '.failure.png'), fullPage: true }); }
  catch (error) { failure = new AggregateError([failure, error], '场景与截图均失败'); }
  if (failure) result.error = String(failure.stack ?? failure);
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
console.log(JSON.stringify({ phase: result.phase, output }));
