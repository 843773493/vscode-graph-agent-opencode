import { writeFile } from 'node:fs/promises';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { launchElectron } from './launch.mjs';

const output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!output) throw new Error('Electron Quick Access 缺少结果路径');
const owner = JSON.parse(process.env.BOXTEAM_NATIVE_SEARCH_OWNER ?? 'null');
if (!owner?.gateway_id || !owner?.workspace_id) throw new Error('Electron Quick Access 缺少工作区身份');
const otherOwner = JSON.parse(process.env.BOXTEAM_NATIVE_SEARCH_OTHER_OWNER ?? 'null');
if (!otherOwner?.gateway_id || !otherOwner?.workspace_id) throw new Error('Electron Quick Access 缺少第二范围身份');
const run = TestRunContext.fromTestFile('tests/integration/clients/electron/test_native_quick_access.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], searchRequests: [],
  boundary: 'Linux 真实 development ASAR、Gateway 与工作区文件 owner' };
let page, failure;
const saveReceipt = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const api = (path, init = {}) => page.evaluate(async ({ path, init }) => {
  const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
  const response = await fetch(path, { ...init, headers: {
    'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...init.headers,
  } });
  const body = await response.json();
  if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) {
    throw new Error(JSON.stringify({ path, status: response.status, body }));
  }
  return body;
}, { path, init });
const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
  await globalThis.workbench.commands.executeCommand(id, ...args);
}, { id, args });

try {
  const host = await launchElectron({ run, result, usePage(value) {
    page = value;
    page.on('pageerror', error => result.pageErrors.push(String(error)));
    page.on('request', request => {
      if (new URL(request.url()).pathname !== '/api/gateway/search') return;
      const body = request.postDataJSON(), headers = request.headers();
      const owners = body.scope.mode === 'resume' ? body.scope.scope.owners : body.scope.owners;
      result.searchRequests.push({ query: body.queries[0].query, owners: owners.map(item => item.workspace_id).sort(),
        request_id: headers['x-request-id'], request });
    });
    page.on('response', response => {
      if (new URL(response.url()).pathname !== '/api/gateway/search') return;
      const match = result.searchRequests.find(item => item.request === response.request());
      if (match) { match.request_id = response.headers()['x-request-id']; delete match.request; }
    });
  } });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const active = (await api('/api/gateway/workspaces')).data.active_workspace;
  expect(active.workspace_id).toBe(owner.workspace_id);
  const headers = { 'X-BoxTeam-Workspace-Id': owner.workspace_id,
    'X-BoxTeam-Target-Gateway-Id': owner.gateway_id };
  result.phase = 'native-shortcuts-and-scoped-search';
  await page.bringToFront();
  await page.keyboard.press('Control+P');
  let quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await expect(quick).toBeVisible();
  await expect(input).toBeFocused();
  await input.fill('%native quick access scope token');
  const match = quick.getByRole('option').filter({ hasText: 'native-quick-access.txt' });
  await expect(match).toBeVisible();
  await expect.poll(() => result.searchRequests.some(item => item.query === 'native quick access scope token')).toBe(true);
  expect(result.searchRequests.filter(item => item.query === 'native quick access scope token')
    .every(item => item.owners.includes(owner.workspace_id) && item.request_id)).toBe(true);
  await input.press('Enter');
  await expect(quick).not.toBeVisible();
  await expect(page.locator('.editor-instance .view-lines')).toContainText('native quick access scope token');
  result.quickAccessShortcut = true;
  result.scopedContentSearch = true;
  await saveReceipt();

  result.phase = 'native-explicit-multi-workspace-scope';
  await command('workbench.action.quickOpen', 'native-quick-access.txt');
  await expect(quick.getByRole('option').filter({ hasText: 'native-quick-access.txt' })).toBeVisible();
  await quick.getByRole('button', { name: '选择搜索范围' }).click();
  await expect(quick).toContainText('选择快速访问搜索范围');
  await quick.locator('.monaco-list-row').filter({ hasText: otherOwner.workspace_id }).getByRole('checkbox').check();
  await quick.getByRole('button', { name: '确定' }).click();
  await expect(quick).toContainText('native-quick-access.txt');
  await input.fill('?');
  await quick.getByRole('option', { name: /^%,/ }).click();
  await input.fill('%native quick access scope token');
  await expect(quick).toContainText('other native quick access scope token');
  await expect.poll(() => result.searchRequests.some(item => item.query === 'native quick access scope token'
    && item.owners.length === 2)).toBe(true);
  const multi = result.searchRequests.filter(item => item.query === 'native quick access scope token' && item.owners.length === 2);
  expect(multi.every(item => item.owners.join('|') === [owner.workspace_id, otherOwner.workspace_id].sort().join('|') && item.request_id)).toBe(true);
  await page.keyboard.press('Escape');
  result.explicitScopeSearch = true;
  await saveReceipt();

  result.phase = 'native-command-center-and-management';
  await page.locator('.command-center-quick-pick').click();
  quick = page.locator('.quick-input-widget'); input = quick.locator('input').first();
  await expect(quick).toBeVisible();
  await page.keyboard.press('Escape');
  result.commandCenterOpensQuickAccess = true;
  await saveReceipt();
  await host.leave();
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = error?.stack ?? String(error);
} finally {
  result.phase = failure ? 'failed' : result.phase;
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { failure ??= error; result.writeError = String(error?.stack ?? error); }
  try { await run.close(); }
  catch (error) { failure ??= error; }
}
if (failure) throw failure;
