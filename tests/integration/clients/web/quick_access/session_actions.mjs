// Integration：真实 Gateway／Backend、正式 Workbench 和原目录 outbox，无响应替身或历史预写。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  if (!process.env[name]) throw new Error(`会话管理验收缺少 ${name}`);
  return process.env[name];
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_session_actions.py');
const result = { phase: 'prepare', pageErrors: [], writes: [], reads: [], receipts: [], forbiddenWrites: [],
  boundary: '真实Gateway／Backend、正式Workbench和原React对话框／目录outbox；产品API创建会话；无模型调用、响应替身或历史预写' };
let page, failure;
const checkpoint = async phase => { result.phase = phase; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('session-actions-browser', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1500, height: 1050 } });
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    const entry = { phase: result.phase, path, method: request.method() };
    if (path === '/api/v1/session-catalog/operations:enqueue') result.writes.push({ ...entry, body: request.postDataJSON() });
    if (/\/sessions\//.test(path)) result.reads.push(entry);
    if (request.method() !== 'GET' && (/\/(messages|jobs)(\/|$)/.test(path) || /\/api\/gateway\/workspaces\/[^/]+\/(activate|reconnect|runtime\/)/.test(path))) result.forbiddenWrites.push(entry);
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = async (path, owner, body) => {
    const receipt = await page.evaluate(async ({ path, owner, body }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
        body: body === undefined ? undefined : JSON.stringify(body), headers: { 'X-Local-Token': credential.data.token,
          'Content-Type': 'application/json', ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) } });
      return { status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, owner, body });
    expect(receipt.body.request_id).toBe(receipt.header);
    result.receipts.push({ phase: result.phase, path, status: receipt.status, request_id: receipt.body.request_id });
    return receipt;
  };
  const data = async (path, owner, body) => {
    const receipt = await api(path, owner, body); expect(receipt.status).toBe(200); return receipt.body.data;
  };
  const health = await data('/api/gateway/health'), routes = await data('/api/gateway/workspaces?check_health=false');
  const owner = routes.active_workspace, workspace = await data('/api/v1/workspace');
  expect(owner.gateway_id).toBe(health.gateway_id); expect(workspace.workspace_id).toBe(owner.workspace_id);
  result.owner = owner;
  const sessions = [];
  for (const title of ['Quick Session Alpha', 'Quick Session Beta']) sessions.push(await data('/api/v1/sessions', owner, { title }));
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const thread = await data(`/api/v1/sessions/${sessions[0].session_id}/main-thread`, owner);
  await command('_workbench.open', `boxteam://${owner.gateway_id}/workspace/${owner.workspace_id}/resources/session/${sessions[0].session_id}/threads/${thread.thread_id}`);
  await expect(page.locator(`section.boxteam-thread[data-session-id="${sessions[0].session_id}"] textarea[data-testid="composer-input"]`).filter({ visible: true })).toBeEnabled({ timeout: 30_000 });
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const session = index => data(`/api/v1/sessions/${sessions[index].session_id}`, owner);
  const search = async title => {
    await command('workbench.action.quickOpen', `session ${title}`);
    await expect(input).toHaveValue(`session ${title}`);
    const row = quick.locator('.monaco-list-row').filter({ hasText: title }).filter({ hasText: sessions[1].session_id });
    await expect(row).toBeVisible(); return row;
  };
  const nameDialog = page.getByRole('dialog', { name: '重命名会话', exact: true });
  const rename = async title => {
    await expect(nameDialog).toBeVisible(); await nameDialog.getByLabel('会话名称', { exact: true }).fill(title);
    await nameDialog.getByRole('button', { name: '保存', exact: true }).click();
    await expect(nameDialog).toBeHidden(); await expect.poll(async () => (await session(1)).title).toBe(title);
  };
  const phaseWrites = phase => result.writes.filter(item => item.phase === phase);

  await checkpoint('unopened-rename');
  const candidate = await search('Quick Session Beta');
  await candidate.hover(); await candidate.getByRole('button', { name: '重命名会话', exact: true }).click();
  await expect(nameDialog).toBeVisible(); expect(phaseWrites(result.phase)).toEqual([]);
  await rename('Quick Session Renamed');
  expect(result.reads.some(item => item.phase === result.phase && item.path.includes(sessions[1].session_id) && /\/(main-thread|threads)(\/|$)/.test(item.path))).toBe(false);
  expect(phaseWrites(result.phase)).toHaveLength(1);
  expect(phaseWrites(result.phase)[0].body.intents[0].kind).toBe('rename_node');
  result.unopenedRename = true;

  await checkpoint('original-menu');
  const treeRow = page.getByTestId(`catalog-node-${JSON.stringify([owner.gateway_id, owner.workspace_id])}-${sessions[1].session_id}`);
  await expect(treeRow).toContainText('Quick Session Renamed');
  await treeRow.click({ button: 'right' }); await page.getByRole('menuitem', { name: '重命名', exact: true }).click();
  await rename('Quick Session Final');
  expect(phaseWrites(result.phase)).toHaveLength(1); result.originalMenu = true;

  await checkpoint('selection-cancel');
  await command('workbench.action.closeAllEditors');
  // 命令等待原对话框结算；只观察 Promise，不在页面内替产品确认或执行写入。
  await page.evaluate(() => { globalThis.sessionActionDone = false; globalThis.sessionActionFailure = undefined;
    void globalThis.workbench.commands.executeCommand('boxteam.session.rename').then(() => { globalThis.sessionActionDone = true; }, cause => { globalThis.sessionActionFailure = String(cause); }); });
  await expect(input).toBeVisible(); await input.fill('session Quick Session Alpha');
  const selecting = quick.locator('.monaco-list-row').filter({ hasText: 'Quick Session Alpha' }).filter({ hasText: sessions[0].session_id });
  await expect(selecting).toBeVisible(); expect(await selecting.locator('.quick-input-list-entry-action-bar .action-item').count()).toBe(0);
  await selecting.click(); await expect(nameDialog).toBeVisible();
  expect(await page.evaluate(() => globalThis.sessionActionDone)).toBe(false);
  await nameDialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect.poll(() => page.evaluate(() => globalThis.sessionActionDone)).toBe(true);
  expect(await page.evaluate(() => globalThis.sessionActionFailure)).toBeUndefined();
  expect(phaseWrites(result.phase)).toEqual([]); result.selectionCancel = true;

  await checkpoint('delete-cancel');
  let row = await search('Quick Session Final'); await row.hover(); await row.getByRole('button', { name: '永久删除会话', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: '永久删除会话', exact: true });
  await expect(confirmation).toBeVisible(); await expect(confirmation).toContainText('Quick Session Final');
  await confirmation.getByRole('button', { name: '取消', exact: true }).click();
  expect(phaseWrites(result.phase)).toEqual([]); expect((await session(1)).title).toBe('Quick Session Final'); result.deleteCancel = true;

  await checkpoint('delete');
  row = await search('Quick Session Final'); await row.hover(); await row.getByRole('button', { name: '永久删除会话', exact: true }).click();
  await expect(confirmation).toBeVisible(); await confirmation.getByRole('button', { name: '删除', exact: true }).click();
  await expect.poll(async () => (await api(`/api/v1/sessions/${sessions[1].session_id}`, owner)).status).toBe(404);
  expect(phaseWrites(result.phase)).toHaveLength(1); expect(phaseWrites(result.phase)[0].body.intents[0].kind).toBe('delete_session');
  expect((await session(0)).title).toBe('Quick Session Alpha'); result.deleted = true;
  expect(result.forbiddenWrites).toEqual([]); expect(result.pageErrors).toEqual([]);
  await checkpoint('complete');
} catch (cause) { failure = cause; }
finally {
  if (failure) {
    result.error = String(failure.stack ?? failure);
    if (page) await page.screenshot({ path: output.replace(/\.json$/, '.failure.png'), fullPage: true });
  }
  await writeFile(output, JSON.stringify(result, null, 2) + '\n'); await run.close();
}
if (failure) throw failure;
console.log(JSON.stringify({ phase: result.phase, output }));
