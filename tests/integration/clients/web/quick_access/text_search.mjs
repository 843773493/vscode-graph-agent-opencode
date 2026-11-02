import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { verifySearchEditors } from './search_editor.mjs';
import { verifySearchContext } from './search_context.mjs';
import { verifySearchRecovery } from './search_recovery.mjs';
import { verifyFileWatch } from './file_watch.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshotPath = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
const otherWorkspace = process.env.BOXTEAM_SEARCH_OTHER_WORKSPACE;
const replacementWorkspace = process.env.BOXTEAM_SEARCH_REPLACEMENT_WORKSPACE;
const workspacePath = process.env.BOXTEAM_SEARCH_WORKSPACE;
if (!baseUrl || !resultPath || !screenshotPath || !otherWorkspace || !replacementWorkspace || !workspacePath) throw new Error('文件快速搜索缺少运行参数');
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
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...init.headers } });
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
  // 静态 IndexedDB 文件是查询装具；在工作台创建目录树之前准备，编辑仍走原入口。
  await page.goto(new URL('/api/gateway/health', baseUrl).href, { waitUntil: 'domcontentloaded' });
  await page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('boxteam-workbench-files', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('files');
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try {
      await new Promise((resolve, reject) => {
        const tx = db.transaction('files', 'readwrite');
        tx.objectStore('files').put(new TextEncoder().encode('virtual-disk quickscope\n'), '/Workspace/quick-scope.txt');
        const legacy = '# Query: quickscope\n# Flags: IgnoreExcludeSettings\n\n1 result - 1 file\n\nquick-scope.txt:\n  1: legacy-display-only\n';
        tx.objectStore('files').put(new TextEncoder().encode(legacy), '/Workspace/legacy.code-search');
        tx.objectStore('files').put(new TextEncoder().encode('# Query: quickscope\n# BoxTeamScope: {"version":99,"kind":"native"}\n\nold results'), '/Workspace/unknown.code-search');
        tx.objectStore('files').put(new TextEncoder().encode('# Query: quickscope\n# BoxTeamScope: {broken\n\nold results'), '/Workspace/broken.code-search');
        tx.oncomplete = resolve; tx.onabort = () => reject(tx.error);
      });
    } finally { db.close(); }
  });
  // 先注册静态目标再加载页面，导航通过正式初始化取得 A/B 两个工作区。
  const registered = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({ root_path: otherWorkspace, name: '搜索隔离 B' }) });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const route = workspaces.data.active_workspace_id;
  const headers = { 'X-BoxTeam-Workspace-Id': route };
  const workspace = await api('/api/v1/workspace', { headers }), roots = await api('/api/v1/workspace/files/roots', { headers });
  result.initialization = [health, workspaces, workspace, roots].map(body => ({ requestId: body.request_id }));
  result.owner = { gatewayId: health.data.gateway_id, workspaceId: workspace.data.workspace_id, route, root: roots.data.workspace_uri };

  result.phase = 'default-target-ignore-rules';
  await command('workbench.action.quickOpen', '%ignorescopeonly');
  const quick = page.locator('.quick-input-widget');
  await expect(quick).toBeVisible();
  await expect(quick).toContainText('visible ignorescopeonly');
  await expect(quick).not.toContainText('hidden ignorescopeonly');
  const ignoredRequests = result.searches.filter(request => request.query === 'ignorescopeonly');
  expect(ignoredRequests.length).toBeGreaterThan(0);
  expect(ignoredRequests.every(request => request.search_options.ignore_mode === 'respect')).toBe(true);
  result.defaultIgnoreRules = true;
  await page.keyboard.press('Escape');

  result.phase = 'configure-through-original-settings-editor';
  await command('workbench.action.openWorkspaceSettingsFile');
  const settings = page.getByRole('dialog', { name: /settings\.json$/ });
  const settingsEditor = settings.locator('.monaco-editor');
  await expect(settingsEditor.locator('.view-lines')).toBeVisible();
  await settingsEditor.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  // 上游 Web 默认延时自动保存；关闭后才能检验搜索本身没有触发保存。
  await page.keyboard.insertText('{\n  "search.useIgnoreFiles": false,\n  "files.autoSave": "off",\n  "search.searchEditor.defaultNumberOfContextLines": 0,\n  "search.searchEditor.reusePriorSearchConfiguration": true\n}\n');
  await command('workbench.action.files.save');
  await command('workbench.action.closeActiveEditor');
  await expect(settings).not.toBeVisible();

  result.phase = 'explicitly-disabled-ignore-rules';
  const beforeDisabled = result.searches.length;
  await command('workbench.action.quickOpen', '%ignorescopeonly');
  await expect(quick).toContainText('visible ignorescopeonly');
  await expect(quick).toContainText('hidden ignorescopeonly');
  const unignoredRequests = result.searches.slice(beforeDisabled).filter(request => request.query === 'ignorescopeonly');
  expect(unignoredRequests.length).toBeGreaterThan(0);
  expect(unignoredRequests.every(request => request.search_options.ignore_mode === 'none')).toBe(true);
  result.disabledIgnoreRules = true;
  await page.keyboard.press('Escape');

  result.phase = 'other-owner-and-native-fixtures';
  const other = registered.data.items.find(item => item.name === '搜索隔离 B');
  if (!other) throw new Error('注册回执缺少 B');
  expect(other.status).toBe('ready');
  const otherHeaders = { 'X-BoxTeam-Workspace-Id': other.workspace_id };
  const otherIdentity = await api('/api/v1/workspace', { headers: otherHeaders });
  const otherRoots = await api('/api/v1/workspace/files/roots', { headers: otherHeaders });
  const otherOwner = { gatewayId: result.owner.gatewayId, workspaceId: otherIdentity.data.workspace_id,
    root: otherRoots.data.workspace_uri, route: other.workspace_id };
  if (otherOwner.workspaceId === result.owner.workspaceId) throw new Error('隔离工作区身份重复');
  result.otherOwner = otherOwner;
  const fileUri = (owner, filename = 'quick-scope.txt') => page.evaluate(({ owner, filename }) => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(owner.root)}/${filename}` }).toString(), { owner, filename });
  await verifyFileWatch({ page, command, result, workspace: workspacePath, resource: await fileUri(result.owner, 'watch-contract.txt'), owner: result.owner });
  const edit = async (resource, original, value) => {
    await command('_workbench.open', resource, [undefined, { pinned: true }]);
    const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
    await expect(editor.locator('.view-lines')).toContainText(original);
    await editor.locator('.view-lines').click();
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.insertText(value);
    await expect(editor.locator('.view-lines')).toContainText(value.trim());
  };
  await edit(await fileUri(otherOwner), 'foreign-disk', 'foreign-buffer quickscope\n');
  await edit('workbench:/Workspace/quick-scope.txt', 'virtual-disk', 'virtual-buffer quickscope\n');

  result.phase = 'dirty-business-file';
  const file = await fileUri(result.owner);
  await command('_workbench.open', file, [undefined, { pinned: true }]);
  const text = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await expect(text.locator('.view-lines')).toContainText('diskword');
  await text.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('dirtyword quickscope 🧪\n');
  await expect(text.locator('.view-lines')).toContainText('dirtyword');

  result.phase = 'cross-workspace-and-native-isolation';
  const otherNavigation = page.getByTestId(`workspace-node-${other.workspace_id}`);
  await otherNavigation.getByRole('button', { name: '搜索隔离 B', exact: true }).click();
  await expect(otherNavigation).toHaveAttribute('aria-current', 'location');
  const switched = await api('/api/gateway/workspaces');
  expect(switched.data.active_workspace_id).toBe(other.workspace_id);
  await command('workbench.action.quickOpen', '%quickscope');
  await expect(quick).toContainText('dirtyword quickscope');
  await expect.poll(() => result.searches.some(request => request.query === 'quickscope')).toBe(true);
  await expect(quick).not.toContainText('foreign-buffer');
  await expect(quick).not.toContainText('virtual-buffer');
  await page.keyboard.press('Escape');
  result.crossWorkspaceIsolation = true;

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
  const rerunRequests = result.searches.length;
  await command('workbench.action.findInFiles', { query: 'quickscope', triggerSearch: true, useExcludeSettingsAndIgnoreFiles: false });
  await expect.poll(() => result.searches.slice(rerunRequests).some(request => request.query === 'quickscope')).toBe(true);
  await expect(searchView.getByRole('tree')).toHaveAttribute('aria-label', /Search: quickscope/);
  await expect(searchView).toContainText('dirtyword quickscope');
  result.viewTransferAndRerun = true;
  const onlyOpenRequests = result.searches.length;
  await command('workbench.action.findInFiles', { query: '🧪', onlyOpenEditors: true, triggerSearch: true, useExcludeSettingsAndIgnoreFiles: false });
  await expect(searchView.getByRole('tree')).toHaveAttribute('aria-label', /Search: 🧪/);
  await expect(searchView).toContainText('dirtyword quickscope');
  await expect(searchView).not.toContainText('foreign-buffer');
  await expect(searchView).not.toContainText('virtual-buffer');
  // A 的唯一已打开文件由本地模型完成；B 与 IndexedDB 不应触发额外精确文件查询。
  await expect(searchView).toContainText('searching only in open files');
  expect(result.searches.slice(onlyOpenRequests)).toEqual([]);
  result.onlyOpenIsolation = true;
  result.phase = 'move-original-search-view';
  await command('vscode.moveViews', { viewIds: ['workbench.view.search'], destinationId: 'workbench.panel.output' });
  await expect(page.locator('.part.panel .search-view')).toBeVisible();
  const beforeMoveQuery = result.searches.length;
  await command('workbench.action.findInFiles', { query: 'dirtyword', onlyOpenEditors: false, triggerSearch: true, useExcludeSettingsAndIgnoreFiles: false });
  await expect.poll(() => result.searches.slice(beforeMoveQuery).some(request => request.query === 'dirtyword')).toBe(true);
  await expect(page.locator('.part.panel .search-view').getByRole('tree')).toHaveAttribute('aria-label', /Search: dirtyword/);
  await expect(page.locator('.part.panel .search-view')).toContainText('dirtyword');
  result.movedViewRerun = true;
  if (result.searches.some(request => request.uri !== result.owner.root || request.route !== route)) throw new Error('文件搜索访问了未选择的目录或路由');
  result.phase = 'reverse-owner-isolation';
  await command('_workbench.open', await fileUri(otherOwner), [undefined, { pinned: true }]);
  const reverseRequests = result.searches.length;
  await command('workbench.action.quickOpen', '%quickscope');
  await expect(quick).toContainText('foreign-buffer quickscope');
  await expect.poll(() => result.searches.slice(reverseRequests).some(request => request.query === 'quickscope')).toBe(true);
  await expect(quick).not.toContainText('dirtyword');
  await expect(quick).not.toContainText('virtual-buffer');
  if (result.searches.slice(reverseRequests).some(request => request.uri !== otherOwner.root || request.route !== otherOwner.route)) throw new Error('反向 B 查询串入其它范围');
  result.reverseOwnerIsolation = true;
  await page.keyboard.press('Escape');
  await verifySearchEditors({ page, command, result, fileUri, ownerA: result.owner, ownerB: otherOwner });
  await verifySearchContext({ page, command, result, fileUri, owner: result.owner });
  await verifySearchRecovery({ page, command, api, result, fileUri, ownerA: result.owner, ownerB: otherOwner,
    oldRoot: otherWorkspace, replacementRoot: replacementWorkspace });
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
