import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshotPath = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!baseUrl || !resultPath || !screenshotPath) throw new Error('文件快速搜索缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_text_search.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], searches: [], initialization: [],
  boundary: '真实 Gateway、托管后端与正式 Workbench；无业务 HTTP 替身，源文件为隔离装具' };
let page, failure;
async function command(id, ...args) {
  await page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
}
async function api(path, init = {}) {
  return page.evaluate(async ({ path, init }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token, ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ status: response.status, body }));
    return body;
  }, { path, init });
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
    if (new URL(request.url()).pathname === '/api/v1/resources/navigation' && request.method() === 'POST') {
      const body = request.postDataJSON();
      if (body.operation === 'search') result.searches.push({ ...body, route: request.headers()['x-boxteam-workspace-id'] });
    }
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const route = workspaces.data.active_workspace_id;
  const headers = { 'X-BoxTeam-Workspace-Id': route };
  const workspace = await api('/api/v1/workspace', { headers }), roots = await api('/api/v1/workspace/files/roots', { headers });
  result.initialization = [health, workspaces, workspace, roots].map(body => ({ requestId: body.request_id }));
  result.owner = { gatewayId: health.data.gateway_id, workspaceId: workspace.data.workspace_id, route, root: roots.data.workspace_uri };

  result.phase = 'explicit-ignore-rejection';
  await command('workbench.action.quickOpen', '%quickscope');
  const quick = page.locator('.quick-input-widget');
  await expect(quick).toBeVisible();
  await expect(quick).toContainText('ignore 文件');
  result.unsupportedIgnoreVisible = true;
  await page.keyboard.press('Escape');

  result.phase = 'configure-through-original-settings-editor';
  await command('workbench.action.openWorkspaceSettingsFile');
  const settings = page.getByRole('dialog', { name: /settings\.json$/ });
  const settingsEditor = settings.locator('.monaco-editor');
  await expect(settingsEditor.locator('.view-lines')).toBeVisible();
  await settingsEditor.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  // 上游 Web 默认延时自动保存；关闭后才能检验搜索本身没有触发保存。
  await page.keyboard.insertText('{\n  "search.useIgnoreFiles": false,\n  "files.autoSave": "off"\n}\n');
  await command('workbench.action.files.save');
  await command('workbench.action.closeActiveEditor');
  await expect(settings).not.toBeVisible();

  result.phase = 'dirty-business-file';
  const file = await page.evaluate(owner => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(owner.root)}/quick-scope.txt` }).toString(), result.owner);
  await command('_workbench.open', file, [undefined, { pinned: true }]);
  const text = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await expect(text.locator('.view-lines')).toContainText('diskword');
  await text.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('dirtyword quickscope 🧪\n');
  await expect(text.locator('.view-lines')).toContainText('dirtyword');

  result.phase = 'original-quick-search';
  await command('workbench.action.quickOpen', '%dirtyword');
  await expect(quick).toBeVisible();
  await expect(quick.locator('input')).toBeFocused();
  await expect(quick.locator('input')).toHaveAttribute('placeholder', /搜索文件内容/);
  await expect(quick).toContainText('dirtyword');
  await expect.poll(() => result.searches.some(request => request.query === 'dirtyword')).toBe(true);
  await quick.locator('input').fill('%diskword');
  await expect(quick).toContainText('No matching results');
  result.dirtyModelSearch = true;
  await quick.locator('input').fill('%dirtyword');
  await expect(quick).toContainText('dirtyword');

  result.phase = 'transfer-and-rerun-original-search-view';
  await quick.locator('.quick-input-header').getByRole('button', { name: 'Open in Search View', exact: true }).click();
  await expect(quick).not.toBeVisible();
  const searchView = page.locator('.search-view');
  await expect(searchView).toBeVisible();
  await expect(searchView).toContainText('dirtyword');
  await command('workbench.action.findInFiles', { query: 'quickscope', triggerSearch: true, useExcludeSettingsAndIgnoreFiles: false });
  await expect.poll(() => result.searches.some(request => request.query === 'quickscope')).toBe(true);
  await expect(searchView).toContainText('dirtyword quickscope');
  result.viewTransferAndRerun = true;
  result.phase = 'move-original-search-view';
  await command('vscode.moveViews', { viewIds: ['workbench.view.search'], destinationId: 'workbench.panel.output' });
  await expect(page.locator('.part.panel .search-view')).toBeVisible();
  const beforeMoveQuery = result.searches.length;
  await command('workbench.action.findInFiles', { query: 'dirtyword', triggerSearch: true, useExcludeSettingsAndIgnoreFiles: false });
  await expect.poll(() => result.searches.slice(beforeMoveQuery).some(request => request.query === 'dirtyword')).toBe(true);
  await expect(page.locator('.part.panel .search-view')).toContainText('dirtyword');
  result.movedViewRerun = true;
  if (result.searches.some(request => request.uri !== result.owner.root || request.route !== route)) throw new Error('文件搜索访问了未选择的目录或路由');
  await expect(page.locator('#startup-error')).toBeEmpty();
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  failure = error;
  if (page) await page.screenshot({ path: screenshotPath }).catch(screenshotError => { failure = new AggregateError([failure, screenshotError], '搜索验收与截图失败'); });
  await writeFile(resultPath, JSON.stringify({ ...result, error: String(failure) }, null, 2)).catch(writeError => { failure = new AggregateError([failure, writeError], '搜索验收与收据失败'); });
} finally {
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '搜索验收与清理失败') : error; }
  if (failure) throw failure;
}
