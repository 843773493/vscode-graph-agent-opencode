import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { fileSearchRequests, searchReceipt } from './federation_observation.mjs';
import { writeWorkspaceSettings } from './settings.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshotPath = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
const other = process.env.BOXTEAM_COVERAGE_OTHER, stopped = process.env.BOXTEAM_COVERAGE_STOPPED;
if (!baseUrl || !resultPath || !screenshotPath || !other || !stopped) throw new Error('模型覆盖验收缺少隔离目标或产物路径');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_file_model_coverage.py');
const result = { classification: 'Integration', phase: 'initialization', pageErrors: [], requests: [], responses: [] };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  page = await browser.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  const observe = request => result.requests.push(...fileSearchRequests(request));
  page.on('request', observe);
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
  const api = (path, init = {}) => page.evaluate(async ({ path, init }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (body.request_id !== response.headers.get('X-Request-ID')) throw new Error('初始化或文件请求丢失 request_id');
    return { status: response.status, body };
  }, { path, init });
  await page.goto(new URL('/api/gateway/health', baseUrl).href);
  const initial = await api('/api/gateway/workspaces');
  expect(initial.status).toBe(200);
  const routeA = initial.body.data.active_workspace_id;
  const registered = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({ root_path: other, name: 'Coverage B' }) });
  const stoppedTarget = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({ root_path: stopped, name: 'Coverage C' }) });
  expect(registered.status).toBe(200); expect(stoppedTarget.status).toBe(200);
  const routeB = registered.body.data.items.find(item => item.name === 'Coverage B').workspace_id;
  const routeC = stoppedTarget.body.data.items.find(item => item.name === 'Coverage C').workspace_id;
  const stop = await api(`/api/gateway/workspaces/${routeC}/runtime/stop`, { method: 'POST' });
  expect(stop.status).toBe(200); expect(stop.body.data.status).toBe('stopped');
  const active = await api(`/api/gateway/workspaces/${routeA}/activate`, { method: 'POST' });
  expect(active.status).toBe(200);
  await page.goto(baseUrl);
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health');
  const listing = await api('/api/gateway/workspaces');
  expect(health.status).toBe(200); expect(listing.status).toBe(200);
  const targetA = listing.body.data.items.find(target => target.workspace_id === routeA);
  if (!targetA) throw new Error('没有找到独立的 A 目标');
  const targets = [];
  for (const [route, label] of [[targetA.workspace_id, 'Coverage A'], [routeB, 'Coverage B']]) {
    const headers = { 'X-BoxTeam-Workspace-Id': route };
    const workspace = await api('/api/v1/workspace', { headers });
    const roots = await api('/api/v1/workspace/files/roots', { headers });
    expect(workspace.status).toBe(200); expect(roots.status).toBe(200);
    targets.push({ gatewayId: health.body.data.gateway_id, workspaceId: workspace.body.data.workspace_id,
      rootUri: roots.body.data.workspace_uri, label, route });
  }
  expect(targets[0].workspaceId).not.toBe(targets[1].workspaceId);
  result.targets = targets;
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off', 'search.maxResults': 2, 'search.useIgnoreFiles': false });
  const resource = owner => page.evaluate(owner => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(owner.rootUri)}/a.coverage.txt` }).toString(), owner);
  result.phase = 'edit-through-real-text-editor';
  await command('_workbench.open', await resource(targets[0]), [undefined, { pinned: true }]);
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await expect(editor.locator('.view-lines')).toContainText('coverageword');
  await editor.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('only in A draft, with no search match');
  await expect(editor.locator('.view-lines')).toContainText('only in A draft');
  result.phase = 'original-search-editor-only-a';
  const waitOnlyA = page.waitForRequest(request => new URL(request.url()).pathname === '/api/gateway/search'
    && request.method() === 'POST' && request.postDataJSON().queries.some(query => query.category === 'file' && query.query === 'coverageword'));
  await command('search.action.openEditor', { query: 'coverageword', location: 'new', triggerSearch: true,
    scope: { version: 1, kind: 'workspace', workspaces: [targets[0]] }, onlyOpenEditors: false, contextLines: 0,
    useExcludeSettingsAndIgnoreFiles: false, filesToInclude: '*.coverage.txt' });
  const onlyAReceipt = await searchReceipt(await waitOnlyA);
  expect(onlyAReceipt.status).toBe(200);
  expect(onlyAReceipt.scope.owners.map(owner => owner.workspace_id)).toEqual([targets[0].workspaceId]);
  expect(onlyAReceipt.pages.flatMap(event => event.page.files.matches.map(hit => hit.uri))).toEqual([targets[0].rootUri + '/b.coverage.txt']);
  expect(result.requests.filter(item => item.query === 'coverageword').every(item => item.workspaceId === targets[0].workspaceId)).toBe(true);
  result.onlyAUnqueriedB = true;
  result.phase = 'original-search-editor-explicit-a-b';
  const waitA = page.waitForRequest(request => new URL(request.url()).pathname === '/api/gateway/search'
    && request.method() === 'POST' && request.postDataJSON().queries.some(query => query.category === 'file' && query.query === 'coverageword'));
  await command('search.action.openEditor', { query: 'coverageword', location: 'new', triggerSearch: true,
    scope: { version: 1, kind: 'workspace', workspaces: targets }, onlyOpenEditors: false, contextLines: 0,
    useExcludeSettingsAndIgnoreFiles: false, filesToInclude: '*.coverage.txt' });
  const request = await waitA, receipt = await searchReceipt(request);
  expect(receipt.status).toBe(200);
  expect(receipt.scope.owners.map(owner => owner.workspace_id)).toEqual(targets.map(target => target.workspaceId));
  const initialTargets = request.postDataJSON().queries[0].file_targets;
  expect(initialTargets).toHaveLength(2);
  const targetAQuery = initialTargets.find(item => item.owner.workspace_id === targets[0].workspaceId).target;
  const targetBQuery = initialTargets.find(item => item.owner.workspace_id === targets[1].workspaceId).target;
  expect(targetAQuery.search_options.excluded_resource_uris).toEqual([targets[0].rootUri + '/a.coverage.txt']);
  expect(targetBQuery.search_options.excluded_resource_uris).toEqual([]);
  const pageA = receipt.pages.find(event => event.branch.branch.owner.workspace_id === targets[0].workspaceId).page.files;
  expect(pageA.matches.map(hit => hit.uri)).toEqual([targets[0].rootUri + '/b.coverage.txt']);
  expect(pageA.next_cursor).toBeNull();
  const pageB = receipt.pages.find(event => event.branch.branch.owner.workspace_id === targets[1].workspaceId).page.files;
  expect(pageB.matches[0].uri).toBe(targets[1].rootUri + '/a.coverage.txt');
  expect(pageB.next_cursor).toBeTruthy();
  const search = page.locator('.search-editor').filter({ visible: true }).last();
  await expect(search.locator('.view-lines')).toContainText('A remaining coverageword');
  await expect(search.locator('.view-lines')).toContainText('a.coverage.txt');
  await expect(search.locator('.view-lines')).not.toContainText('only in A draft');
  await expect.poll(() => result.requests.some(item => item.workspaceId === targets[1].workspaceId && item.query === 'coverageword')).toBe(true);
  const requests = result.requests.filter(item => item.query === 'coverageword');
  expect(requests.filter(item => item.workspaceId === targets[1].workspaceId).every(item => item.search_options.excluded_resource_uris.length === 0)).toBe(true);
  expect(requests.every(item => targets.some(target => target.workspaceId === item.workspaceId && target.gatewayId === item.gatewayId))).toBe(true);
  expect(receipt.pages.every(event => event.branch.allocated_results === 1)).toBe(true);
  result.modelCoverage = true; result.samePathDifferentOwner = true; result.stoppedUnqueried = true;
  page.off('request', observe);
  const requestA = { uri: targetAQuery.uri, operation: 'search', query: 'coverageword', search_mode: 'content',
    search_options: targetAQuery.search_options, max_bytes: targetAQuery.max_bytes };
  // 复用真实浏览器请求取得的覆盖集合，验证原文件 owner 的一条配额页，不补造模型 metadata。
  const read = payload => api('/api/v1/resources/navigation', { method: 'POST', headers: { 'X-BoxTeam-Workspace-Id': targets[0].route }, body: JSON.stringify(payload) });
  const small = await read({ ...requestA, limit: 1 });
  expect(small.status).toBe(200);
  expect(small.body.data.matches.map(hit => hit.uri)).toEqual([targets[0].rootUri + '/b.coverage.txt']);
  expect(small.body.data.next_cursor).toBeNull();
  result.smallPage = true;
  const beforeCoverage = await read({ ...requestA, limit: 1, search_options: { ...requestA.search_options, excluded_resource_uris: [] } });
  expect(beforeCoverage.status).toBe(200);
  expect(beforeCoverage.body.data.next_cursor).toBeTruthy();
  const changed = await read({ ...requestA, limit: 1, cursor: beforeCoverage.body.data.next_cursor });
  expect(changed.status).toBe(400);
  expect(changed.body.detail.reason).toBe('invalid_cursor');
  result.staleCoverageCursor = true;
  expect(result.requests.filter(item => item.query === 'coverageword').every(item => targets.some(target => target.workspaceId === item.workspaceId && target.gatewayId === item.gatewayId))).toBe(true);
  const finalRegistry = await api('/api/gateway/workspaces?check_health=false');
  const stillStopped = finalRegistry.body.data.items.find(target => target.workspace_id === routeC);
  expect(stillStopped.status).toBe('offline');
  expect(stillStopped.runtime_action).toBe('start_managed_backend');
  result.responses = { onlyAReceipt, receipt, pageA, pageB, small, beforeCoverage, changed };
  result.phase = 'passed';
} catch (error) {
  failure = error; result.error = String(error?.stack ?? error);
  if (page) await page.screenshot({ path: screenshotPath }).catch(screenshotError => { result.screenshotError = String(screenshotError); });
} finally {
  await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
