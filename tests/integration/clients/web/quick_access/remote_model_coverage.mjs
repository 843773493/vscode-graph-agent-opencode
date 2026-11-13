// Integration：真实双 Gateway／Backend；SSH 为既有 loopback，只有具名出站故障与一次真实回执延迟。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { searchRequest, searchReceipt } from '../../../../support/workbench/search-observation.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_MODEL_COVERAGE_FIXTURE);
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_remote_model_coverage.py');
const result = { classification: 'Integration', phase: 'initialize', requests: [], receipts: [], pageErrors: [], consoleErrors: [], forbiddenWrites: [],
  boundary: '真实双Gateway、独立Backend、相同workspace UUID。SSH配对执行／隧道沿既有loopback替身；retrycoverage仅A的Gateway出站读取显式失败，原匹配／调度／SSE不替换。一次firstprogress真实HTTP回执暂缓，草稿只经原编辑器修改。' };
const pending = [], errors = [];
let page, failure, releaseProgress;
const phase = async value => { result.phase = value; await writeFile(fixture.resultPath, JSON.stringify(result, null, 2) + '\n'); };
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('模型覆盖 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1750, height: 1050 }, permissions: ['clipboard-read', 'clipboard-write'] });
  run.addCleanup('模型覆盖 BrowserContext', () => context.close());
  page = await context.newPage(); page.setDefaultTimeout(20_000);
  run.addCleanup('模型覆盖截图', () => page.screenshot({ path: fixture.screenshotPath }));
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push({ phase: result.phase, text: message.text() }); });
  const phases = new WeakMap();
  page.on('request', request => {
    const observed = searchRequest(request);
    if (observed?.path === '/api/gateway/search') {
      phases.set(request, result.phase); result.requests.push({ ...observed, phase: result.phase });
    }
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && /\/(messages|jobs|text-mutations)(\/|$)/.test(path)) result.forbiddenWrites.push(path);
  });
  page.on('requestfinished', request => {
    if (new URL(request.url()).pathname === '/api/gateway/search') pending.push(searchReceipt(request)
      .then(receipt => result.receipts.push({ ...receipt, phase: phases.get(request) })).catch(error => errors.push(String(error))));
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20_000) });
    const payload = await response.json();
    if (!payload.request_id || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(payload));
    return { status: response.status, payload };
  }, { path, owner, body });
  const commandReady = page.waitForResponse(response => response.url().endsWith('/api/gateway/service-plugins/commands/events')
    && response.status() === 200, { timeout: 30_000 });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await commandReady;
  const owners = fixture.gatewayIds.map(gateway_id => ({ gateway_id, workspace_id: fixture.workspaceId }));
  const health = await api('/api/gateway/health'), registry = await api('/api/gateway/workspaces?check_health=false');
  expect(health.payload.data.gateway_id).toBe(owners[0].gateway_id);
  expect(registry.payload.data.active_workspace).toEqual(owners[0]);
  const targets = [];
  for (const owner of owners) {
    const workspace = await api('/api/v1/workspace', owner), roots = await api('/api/v1/workspace/files/roots', owner);
    expect(workspace.status).toBe(200); expect(workspace.payload.data.workspace_id).toBe(fixture.workspaceId);
    targets.push({ gatewayId: owner.gateway_id, workspaceId: owner.workspace_id, rootUri: roots.payload.data.workspace_uri, label: owner.gateway_id });
  }
  result.initialization = { health, registry, targets };
  const settings = { 'files.autoSave': 'off', 'search.maxResults': 4, 'search.useIgnoreFiles': false,
    'workbench.quickOpen.closeOnFocusLost': false };
  await writeWorkspaceSettings({ page, command }, settings);
  const uri = (index, name = 'a.coverage.txt') => page.evaluate(({ target, name }) => globalThis.workbench.URI.from({
    scheme: 'boxteam-file', authority: target.gatewayId,
    path: `/${target.workspaceId}/${encodeURIComponent(target.rootUri)}/${name}` }).toString(), { target: targets[index], name });
  const edit = async text => {
    await command('_workbench.open', await uri(0), [undefined, { pinned: true }]);
    const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
    await expect(editor.locator('.view-lines')).toBeVisible();
    await editor.locator('.view-lines').click({ position: { x: 15, y: 10 } });
    await page.keyboard.press('ControlOrMeta+A'); await page.keyboard.insertText(text);
    await expect(editor.locator('.view-lines')).toContainText(text.split('\n')[0]);
  };
  await edit('PRIVATE-A-DRAFT with no disk keyword');
  const searchEditor = async (query, selected, onlyOpenEditors = false) => {
    await command('search.action.openEditor', { query, location: 'new', triggerSearch: true,
      scope: { version: 1, kind: 'workspace', workspaces: selected }, onlyOpenEditors, contextLines: 0,
      useExcludeSettingsAndIgnoreFiles: false, filesToInclude: '*.coverage.txt' });
    return page.locator('.search-editor').filter({ visible: true }).last().locator('.view-lines');
  };
  const receipts = value => result.receipts.filter(receipt => receipt.phase === value);
  const requests = value => result.requests.filter(request => request.phase === value);
  const matches = receipt => receipt.pages.flatMap(event => event.page.files.matches);
  await phase('only-a');
  const onlyA = await searchEditor('coverageword', [targets[0]]);
  await expect(onlyA).toContainText('A-LAST');
  await expect.poll(() => receipts('only-a').length).toBe(1);
  expect(receipts('only-a')[0].scope.owners).toEqual([owners[0]]);
  expect(matches(receipts('only-a')[0]).map(hit => hit.uri)).toEqual([
    targets[0].rootUri + '/b.coverage.txt', targets[0].rootUri + '/b.coverage.txt', targets[0].rootUri + '/c.coverage.txt']);
  await expect(onlyA).not.toContainText('A-OLD-DISK'); await expect(onlyA).not.toContainText('B-SAME-PATH'); result.onlyA = true;
  await phase('both-small-quota');
  const both = await searchEditor('coverageword', targets);
  for (const text of ['A-FIRST', 'A-SECOND', 'A-LAST', 'B-SAME-PATH']) await expect(both).toContainText(text);
  await expect.poll(() => receipts('both-small-quota').length).toBe(2);
  const [first, next] = receipts('both-small-quota');
  expect(first.scope.owners).toEqual(owners); expect(next.scope).toEqual(first.scope);
  expect(first.summary.returned_results).toBe(3); expect(next.summary.returned_results).toBe(1);
  expect(first.summary.branches.map(branch => branch.returned_results)).toEqual([2, 1]);
  expect(first.pages.map(event => event.branch.allocated_results)).toEqual([2, 2]);
  const aBranch = first.summary.branches.find(branch => branch.branch.owner.gateway_id === owners[0].gateway_id);
  expect(aBranch.state).toBe('limited'); expect(aBranch.cursor).toBeTruthy();
  expect(next.summary.state).toBe('complete'); expect(next.summary.branches).toHaveLength(1);
  expect(requests('both-small-quota')[1].body.continuations).toEqual([{ branch: aBranch.branch, cursor: aBranch.cursor }]);
  expect(requests('both-small-quota')[1].body.queries[0].limit).toBe(2);
  const initialTargets = requests('both-small-quota')[0].body.queries[0].file_targets;
  expect(initialTargets.map(item => item.owner)).toEqual(owners);
  expect(initialTargets.map(item => item.target.search_options.excluded_resource_uris)).toEqual([[targets[0].rootUri + '/a.coverage.txt'], []]);
  expect(matches(first).concat(matches(next))).toHaveLength(4);
  const bothText = await both.innerText();
  expect((bothText.match(/coverageword/g) ?? [])).toHaveLength(4);
  expect(bothText).not.toContain('A-OLD-DISK'); expect(bothText).not.toContain('PRIVATE-A-DRAFT');
  expect(JSON.stringify(requests('both-small-quota'))).not.toContain('PRIVATE-A-DRAFT'); result.remotePaging = true;
  await phase('changed-coverage-cursor');
  const aTarget = initialTargets[0].target;
  const rejected = await api('/api/v1/resources/navigation', owners[0], { ...aTarget, operation: 'search',
    query: 'coverageword', search_mode: 'content', limit: 2, cursor: aBranch.cursor,
    search_options: { ...aTarget.search_options, excluded_resource_uris: [] } });
  expect(rejected.status).toBe(400); expect(rejected.payload.detail.reason).toBe('invalid_cursor');
  result.cursorRejection = rejected; result.staleCursor = true;
  await phase('only-open-a-with-b-open');
  await command('_workbench.open', await uri(1), [undefined, { pinned: true }]);
  const openB = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await expect(openB.locator('.view-lines')).toContainText('B-SAME-PATH');
  const onlyOpen = await searchEditor('coverageword', [targets[0]], true);
  await expect(onlyOpen).toContainText('无结果');
  await expect(onlyOpen).not.toContainText('B-SAME-PATH'); await expect(onlyOpen).not.toContainText('A-FIRST');
  // 等原 Search Editor 的真实查询结算，不用固定sleep断言零请求。
  await expect(page.locator('.search-editor').filter({ visible: true }).last().locator('.monaco-progress-container.active')).toHaveCount(0);
  expect(requests('only-open-a-with-b-open')).toEqual([]); result.onlyOpenText = await onlyOpen.innerText(); result.onlyOpen = true;
  await command('_workbench.open', await uri(1)); await command('workbench.action.closeActiveEditor');
  await edit('PRIVATE-A-DRAFT\nfirstprogress LOCAL-FIRST');
  await writeWorkspaceSettings({ page, command }, { ...settings, 'search.maxResults': 3 });
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const showBoth = async query => {
    await command('workbench.action.quickOpen', '');
    await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
    for (const owner of owners) await quick.locator('.monaco-list-row').filter({ hasText: owner.gateway_id }).getByRole('checkbox').check();
    await quick.getByRole('button', { name: '确定', exact: true }).click();
    await input.fill('%' + query);
  };
  await phase('local-first-progress');
  const held = Promise.withResolvers(), released = Promise.withResolvers(); releaseProgress = () => released.resolve();
  let delayed = false;
  const delay = async route => {
    if (delayed || !route.request().postDataJSON().queries.some(query => query.query === 'firstprogress')) return route.continue();
    delayed = true; const response = await route.fetch(); held.resolve(); await released.promise; await route.fulfill({ response });
  };
  await page.route('**/api/gateway/search', delay);
  try {
    await showBoth('firstprogress'); await held.promise;
    await expect(quick).toContainText('LOCAL-FIRST'); await expect(quick).not.toContainText('A-REMOTE'); await expect(quick).not.toContainText('B-REMOTE');
    await expect(quick.locator('.quick-input-visible-count')).toHaveText('1 个结果');
    result.firstProgressText = await quick.innerText();
    expect(requests('local-first-progress')[0].body.queries[0].limit).toBe(1);
    expect(requests('local-first-progress')[0].body.max_results).toBe(2);
    releaseProgress();
    await expect(quick).toContainText('A-REMOTE'); await expect(quick).toContainText('B-REMOTE');
    await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
    await expect(quick).not.toContainText('A-TAIL');
    await expect(quick.locator('.monaco-list-row').filter({ hasText: /firstprogress (LOCAL-FIRST|A-REMOTE|B-REMOTE)/ })).toHaveCount(3);
    await expect(quick).toContainText('本次查询覆盖受限');
    await expect(quick.locator('.quick-input-visible-count')).toHaveText('3 个结果');
    await expect.poll(() => receipts('local-first-progress').length).toBe(2);
    expect(receipts('local-first-progress').map(receipt => receipt.summary.returned_results)).toEqual([2, 1]);
    result.finalProgressText = await quick.innerText(); result.firstProgress = true;
    await command('workbench.action.keepEditor'); await input.press('Escape');
  } finally { releaseProgress(); await page.unroute('**/api/gateway/search', delay); }
  await phase('model-version-revokes-old-retry');
  await showBoth('retrycoverage');
  await expect(quick).toContainText('B-SUCCESS'); await expect(quick).toContainText('文件搜索失败：');
  await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
  const failedRow = quick.locator('.monaco-list-row').filter({ hasText: '文件搜索失败：' });
  await failedRow.hover();
  await expect(failedRow.getByRole('button', { name: '重新查询此文件分支', exact: true })).toHaveCount(1);
  await command('workbench.action.keepEditor');
  const oldRequests = requests('model-version-revokes-old-retry').length;
  await edit('PRIVATE-A-NEW-VERSION\nfirstprogress LOCAL-FIRST');
  await expect(quick).toBeVisible();
  await failedRow.hover();
  const retry = failedRow.getByRole('button', { name: '重新查询此文件分支', exact: true });
  await expect(retry).toHaveCount(1); await retry.click();
  await expect(quick).toContainText('已打开文件发生变化');
  await expect(quick.getByRole('button', { name: '重新查询此文件分支', exact: true })).toHaveCount(0);
  expect(requests('model-version-revokes-old-retry')).toHaveLength(oldRequests);
  result.staleRetry = true;
  await Promise.all(pending); expect(errors).toEqual([]);
  expect(result.pageErrors).toEqual([]); expect(result.forbiddenWrites).toEqual([]);
  await phase('complete');
} catch (error) { failure = error; result.error = String(error.stack ?? error); }
finally {
  releaseProgress?.();
  try { await run.close(); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '模型覆盖验收与清理均失败') : cause; }
  if (failure) result.error = String(failure.stack ?? failure);
  try { await writeFile(fixture.resultPath, JSON.stringify(result, null, 2) + '\n'); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '模型覆盖验收与结果保存均失败') : cause; }
}
if (failure) throw failure;
