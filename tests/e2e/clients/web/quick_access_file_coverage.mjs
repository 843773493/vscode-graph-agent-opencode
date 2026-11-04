import { writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { searchRequest, searchReceipt } from '../../../support/workbench/search-observation.mjs';
import { writeWorkspaceSettings } from '../../../support/workbench/settings.mjs';
import { verifyNestedSearchRoots } from './quick_access_nested_roots.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const workspaceB = process.env.BOXTEAM_COVERAGE_WORKSPACE_B;
if (!baseUrl || !resultPath || !workspaceB) throw new Error('文件覆盖验收缺少运行参数');
const run = TestRunContext.fromTestFile('tests/e2e/clients/web/test_quick_access_file_coverage.py');
const result = { classification: 'E2E', phase: 'initialize', pageErrors: [], requests: [], receipts: [],
  boundary: '正式 Workbench、真实 Gateway 与托管后端；用户正文经原编辑器输入，不替换搜索或网络响应' };
const reads = [], errors = [];
const contentSearch = observed => observed?.path === '/api/gateway/search' && observed.query === 'coverageword'
  && observed.body.queries.some(query => query.category === 'file' && query.operation === 'content');
let page, failure;
async function command(id, ...args) {
  await page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
}
async function api(path, init = {}, expectedStatus = 200) {
  return page.evaluate(async ({ path, init, expectedStatus }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (response.status !== expectedStatus || !body.request_id || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ status: response.status, body }));
    }
    return body;
  }, { path, init, expectedStatus });
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
    const observed = searchRequest(request);
    if (contentSearch(observed)) {
      result.requests.push({ phase: result.phase, ...observed });
    }
  });
  page.on('requestfinished', request => {
    const observed = searchRequest(request);
    if (contentSearch(observed)) {
      const phase = result.phase;
      reads.push(searchReceipt(request).then(receipt => result.receipts.push({ phase, ...receipt })).catch(error => errors.push(String(error))));
    }
  });
  await page.goto(new URL('/api/gateway/health', baseUrl).href, { waitUntil: 'domcontentloaded' });
  await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({ root_path: workspaceB, name: '覆盖隔离 B' }) });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const ownerA = workspaces.data.active_workspace;
  const headers = { 'X-BoxTeam-Workspace-Id': ownerA.workspace_id, 'X-BoxTeam-Target-Gateway-Id': ownerA.gateway_id };
  const workspace = await api('/api/v1/workspace', { headers });
  const roots = await api('/api/v1/workspace/files/roots', { headers });
  const b = workspaces.data.items.find(item => item.name === '覆盖隔离 B');
  expect(b.status).toBe('ready');
  result.initialization = [health, workspaces, workspace, roots].map(body => body.request_id);
  expect(workspace.data.workspace_id).toBe(ownerA.workspace_id);
  const ownerB = { gateway_id: b.gateway_id, workspace_id: b.workspace_id };
  result.owners = [ownerA, ownerB];
  const root = roots.data.workspace_uri;
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off', 'search.useIgnoreFiles': false, 'search.maxResults': 2 });
  const file = await page.evaluate(({ ownerA, root }) => globalThis.workbench.URI.from({ scheme: 'boxteam-file',
    authority: ownerA.gateway_id, path: `/${ownerA.workspace_id}/${encodeURIComponent(root)}/00-dirty.txt` }).toString(), { ownerA, root });
  await command('_workbench.open', file, [undefined, { pinned: true }]);
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await expect(editor.locator('.view-lines')).toContainText('A disk coverageword');
  await editor.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('A local removed query\n');
  await expect(editor.locator('.view-lines')).toContainText('A local removed query');

  result.phase = 'only-A';
  const quick = page.locator('.quick-input-widget');
  await command('workbench.action.quickOpen', '%coverageword');
  await expect(quick).toContainText('A first coverageword');
  await expect(quick).toContainText('A tail coverageword');
  await expect(quick).not.toContainText('A disk coverageword');
  await expect(quick).not.toContainText('B disk coverageword');
  await expect.poll(() => result.receipts.filter(item => item.phase === 'only-A').length).toBe(1);
  const onlyA = result.requests.filter(item => item.phase === 'only-A');
  expect(onlyA).toHaveLength(1);
  expect(onlyA[0].body.scope.owners).toEqual([ownerA]);
  expect(onlyA[0].body.queries[0].file_targets[0].target.search_options.excluded_resource_uris).toEqual([root + '/00-dirty.txt']);
  const first = result.receipts.find(item => item.phase === 'only-A');
  expect(first.summary.returned_results).toBe(2);
  expect(first.pages.flatMap(event => event.page.files.matches).map(hit => hit.uri)).toEqual([root + '/10-first.txt', root + '/20-tail.txt']);
  result.singleOwnerNegativeCoverage = true;
  await page.keyboard.press('Escape');
  // 原正文 picker 会预览命中；下一轮只保留需要验证的 dirty 模型。
  await command('workbench.action.closeUnmodifiedEditors');
  await command('_workbench.open', file, [undefined, { pinned: true }]);
  await expect(editor.locator('.view-lines')).toContainText('A local removed query');

  result.phase = 'A+B';
  await command('workbench.action.quickOpen', 'coverageword');
  await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
  await expect(quick).toContainText('选择快速访问搜索范围');
  await quick.locator('.monaco-list-row').filter({ hasText: ownerB.gateway_id })
    .filter({ hasText: ownerB.workspace_id }).getByRole('checkbox').click();
  await quick.getByRole('button', { name: 'OK', exact: true }).click();
  await quick.locator('input').first().fill('?');
  await quick.getByRole('option', { name: /^%,/ }).click();
  await quick.locator('input').first().fill('%coverageword');
  await expect(quick).toContainText('A first coverageword');
  await expect(quick).toContainText('B disk coverageword');
  await expect(quick).not.toContainText('A disk coverageword');
  await expect.poll(() => result.receipts.filter(item => item.phase === 'A+B').length).toBe(2);
  const both = result.requests.filter(item => item.phase === 'A+B');
  expect(both).toHaveLength(2);
  const [initial, continuation] = both.map(item => item.body);
  expect(initial.queries[0].limit).toBe(1);
  expect(initial.max_results).toBe(2);
  expect(initial.scope.owners).toEqual([ownerA, ownerB]);
  expect(initial.queries[0].file_targets.map(item => item.target.search_options.excluded_resource_uris)).toEqual([[root + '/00-dirty.txt'], []]);
  expect(continuation.scope.mode).toBe('resume');
  expect(continuation.continuations).toHaveLength(1);
  expect(continuation.continuations[0].branch.owner).toEqual(ownerA);
  const pages = result.receipts.filter(item => item.phase === 'A+B').flatMap(item => item.pages);
  expect(pages.flatMap(event => event.page.files.matches).map(hit => hit.text).sort()).toEqual([
    'A first coverageword', 'A tail coverageword', 'B disk coverageword',
  ].sort());
  expect(result.receipts.filter(item => item.phase === 'A+B').map(item => item.summary.returned_results).sort()).toEqual([1, 2]);
  result.smallQuotaAndContinuation = true;
  await page.keyboard.press('Escape');

  result.phase = 'cursor-coverage-changed';
  const target = initial.queries[0].file_targets.find(item => item.owner.gateway_id === ownerA.gateway_id && item.owner.workspace_id === ownerA.workspace_id).target;
  const rejected = await api('/api/v1/search', { method: 'POST', headers, body: JSON.stringify({
    workspace_id: ownerA.workspace_id, category: 'file', operation: 'content', query: 'coverageword', match_mode: 'literal',
    limit: 1, cursor: continuation.continuations[0].cursor,
    file: { ...target, search_options: { ...target.search_options, excluded_resource_uris: [] } },
  }) }, 400);
  expect(rejected.detail.owner).toBe('resource_file_operations');
  expect(rejected.detail.code).toBe('execution_failed');
  expect(rejected.detail.reason).toBe('invalid_cursor');
  result.cursorRejection = rejected;
  result.coverageBindsCursor = true;
  expect(JSON.stringify(result.requests)).not.toContain('A local removed query');
  await verifyNestedSearchRoots({ page, command,
    target: { gatewayId: ownerA.gateway_id, workspaceId: ownerA.workspace_id, rootUri: root, label: 'Partition' }, result });
  expect(errors).toEqual([]);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  if (page) await page.screenshot({ path: join(dirname(resultPath), 'file-coverage-failure.png') });
  result.error = String(error);
} finally {
  await Promise.all(reads);
  await writeFile(resultPath, JSON.stringify({ ...result, receiptErrors: errors }, null, 2));
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '文件覆盖验收与清理失败') : error; }
  if (failure) throw failure;
}
