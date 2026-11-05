// Integration：模型回放仅供后端启动，JSON 分析使用原 Web 扩展及真实 Worker。
import { dirname, join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`语言模型验收缺少 ${name}`);
  return value;
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_language_models.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], workerScripts: [], reads: [],
  boundary: '真实Gateway／后端／正式Workbench／原JSON扩展和Worker；无响应替身，未预写模型或分析结果，模型回放仅供后端启动' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('language-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1050 } });
  run.addCleanup('language-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  context.on('response', response => {
    if (/\/(extensionHostWorkerMain|jsonServerMain)\.js(?:\?|$)/.test(response.url())) {
      result.workerScripts.push({ url: response.url(), status: response.status() });
    }
  });
  context.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/workspace/files/raw') && url.searchParams.get('path')?.endsWith('/same.json')) {
      result.reads.push({ phase: result.phase, uri: url.searchParams.get('path'),
        workspaceId: request.headers()['x-boxteam-workspace-id'], gatewayId: request.headers()['x-boxteam-target-gateway-id'] });
    }
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, init = {}) => page.evaluate(async ({ path, init }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, body }));
    }
    return body.data;
  }, { path, init });
  const headers = target => ({ 'X-BoxTeam-Target-Gateway-Id': target.gateway_id, 'X-BoxTeam-Workspace-Id': target.workspace_id });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  const targetA = listing.active_workspace;
  expect(targetA.gateway_id).toBe(health.gateway_id);
  const registered = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({
    root_path: required('BOXTEAM_LANGUAGE_OTHER_WORKSPACE'), name: 'Language B',
  }) });
  const targetB = registered.items.find(item => item.name === 'Language B');
  expect(targetB).toBeTruthy();
  const targets = [];
  for (const target of [targetA, targetB]) {
    const workspace = await api('/api/v1/workspace', { headers: headers(target) });
    const roots = await api('/api/v1/workspace/files/roots', { headers: headers(target) });
    expect(workspace.workspace_id).toBe(target.workspace_id);
    targets.push({ gatewayId: target.gateway_id, workspaceId: target.workspace_id, rootUri: roots.workspace_uri });
  }
  result.targets = targets;
  expect(targets[0].workspaceId).not.toBe(targets[1].workspaceId);
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const uri = target => page.evaluate(target => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: target.gatewayId,
    path: `/${target.workspaceId}/${encodeURIComponent(target.rootUri)}/same.json` }).toString(), target);
  const resources = await Promise.all(targets.map(uri));
  const symbols = resource => page.evaluate(async resource => {
    // vscode.* 是扩展宿主别名；工作台命令服务使用它映射的原注册入口。
    const symbols = await globalThis.workbench.commands.executeCommand('_executeDocumentSymbolProvider', globalThis.workbench.URI.parse(resource));
    return symbols?.map(symbol => symbol.name) ?? [];
  }, resource);
  const groups = page.locator('.editor-group-container');
  const editor = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true });
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off' });
  // 先通过原命令关闭初始 README，之后才能把关闭 A 视为释放该组最后一个编辑器。
  await command('workbench.action.closeAllEditors');
  await expect(groups).toHaveCount(1);
  await expect(groups.getByRole('tab')).toHaveCount(0);
  result.phase = 'open-two-owner-models';
  await command('_workbench.open', resources[0], [undefined, { pinned: true }]);
  await expect(editor(0).locator('.view-lines')).toContainText('alphaOwner');
  await command('_workbench.open', resources[1], [1, { pinned: true }]);
  await expect(groups).toHaveCount(2);
  await expect(editor(1).locator('.view-lines')).toContainText('betaOwner');
  await expect.poll(() => symbols(resources[0]), { timeout: 30_000 }).toEqual(['alphaOwner']);
  await expect.poll(() => symbols(resources[1]), { timeout: 30_000 }).toEqual(['betaOwner']);
  result.initial = await Promise.all(resources.map(symbols));
  for (const target of targets) {
    expect(result.reads.some(read => read.uri === target.rootUri + '/same.json'
      && read.workspaceId === target.workspaceId && read.gatewayId === target.gatewayId)).toBe(true);
  }
  result.phase = 'edit-real-model';
  await editor(0).getByRole('textbox').focus();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('{"alphaAfter":true}\n');
  await expect(editor(0).locator('.view-lines')).toContainText('alphaAfter');
  await expect(groups.nth(0)).toHaveClass(/\bactive\b/);
  await expect.poll(() => symbols(resources[0])).toEqual(['alphaAfter']);
  await expect.poll(() => symbols(resources[1])).toEqual(['betaOwner']);
  result.edited = await Promise.all(resources.map(symbols));
  await expect(editor(1).locator('.view-lines')).toContainText('betaOwner');
  result.phase = 'release-and-reopen';
  await command('workbench.action.revertAndCloseActiveEditor');
  await expect(groups).toHaveCount(1);
  await expect(editor(0).locator('.view-lines')).toContainText('betaOwner');
  await command('_workbench.open', resources[0], [1, { pinned: true }]);
  await expect(groups).toHaveCount(2);
  await expect(editor(1).locator('.view-lines')).toContainText('alphaOwner');
  await expect.poll(() => symbols(resources[0])).toEqual(['alphaOwner']);
  result.reopened = await symbols(resources[0]);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error);
  if (page) {
    try { await page.screenshot({ path: join(dirname(output), 'language-models-failure.png'), fullPage: true }); }
    catch (diagnostic) { failure = new AggregateError([failure, diagnostic], '语言模型验收与截图失败'); }
  }
} finally {
  try { await writeFile(output, JSON.stringify(result, null, 2)); }
  catch (diagnostic) { failure = failure ? new AggregateError([failure, diagnostic], '语言模型结果保存失败') : diagnostic; }
  try { await run.close(); }
  catch (cleanup) { failure = failure ? new AggregateError([failure, cleanup], '语言模型验收与清理失败') : cleanup; }
}
if (failure) throw failure;
