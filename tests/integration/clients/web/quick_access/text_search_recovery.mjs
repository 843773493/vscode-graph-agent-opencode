import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { searchRequest, searchReceipt } from '../../../../support/workbench/search-observation.mjs';
import { searchFault } from './query_faults.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const scenario = process.env.BOXTEAM_TEXT_SEARCH_SCENARIO ?? 'recovery';
if (!base || !output || !process.env.BOXTEAM_RECOVERY_WORKSPACE_B) throw new Error('文本分支恢复缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_text_search_recovery.py');
const result = { scenario, phase: 'initialize', pageErrors: [], requests: [], receipts: [], searchResponses: [], initialization: [], modelCalls: [],
  boundary: '真实Gateway/Workspace文件owner与正式Workbench；Gateway出站边界按精确workspace注入失败、取消或暂缓真实响应；身份场景每阶段只在产品HTTP读取边界注入一次401，不替换搜索调度、匹配、SSE或客户端状态' };
const reads = [], readErrors = [];
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext(); run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const observed = searchRequest(request);
    if (observed) result.requests.push(observed);
    if (request.method() === 'POST' && /\/(messages|jobs)(\/|$)/.test(new URL(request.url()).pathname)) result.modelCalls.push(request.url());
  });
  page.on('requestfinished', request => {
    if (new URL(request.url()).pathname === '/api/gateway/search') reads.push(searchReceipt(request)
      .then(receipt => result.receipts.push(receipt)).catch(error => readErrors.push(String(error))));
  });
  page.on('response', response => {
    if (new URL(response.url()).pathname === '/api/gateway/search') result.searchResponses.push({
      query: searchRequest(response.request()).query, requestId: response.headers()['x-request-id'], status: response.status() });
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId("workspace-navigation-root-drop-target")).toBeVisible({ timeout: 30_000 });
  const api = (path, data) => page.evaluate(async ({ path, data }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: data ? 'POST' : 'GET', body: data ? JSON.stringify(data) : undefined,
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return body;
  }, { path, data });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    const body = await api(path); result.initialization.push({ path, requestId: body.request_id });
  }
  const session = (await api('/api/v1/sessions', { title: '文本分支恢复' })).data;
  await command('_workbench.open', session.uri);
  const initial = (await api('/api/gateway/workspaces?check_health=false')).data, a = initial.active_workspace;
  await api('/api/gateway/managed-workspaces', { root_path: process.env.BOXTEAM_RECOVERY_WORKSPACE_B, name: '文本恢复B' });
  const b = (await api('/api/gateway/workspaces?check_health=false')).data.items.find(item => item.name === '文本恢复B');
  expect(b.status).toBe('ready'); expect(b.workspace_id).not.toBe(a.workspace_id);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId("workspace-navigation-root-drop-target")).toBeVisible();
  await command('workbench.action.quickOpen', '');
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
  await expect(quick).toContainText('选择快速访问搜索范围');
  await quick.locator('.monaco-list-row').filter({ hasText: b.workspace_id }).getByRole('checkbox').check();
  await quick.getByRole('button', { name: '确定', exact: true }).click();
  const idle = () => expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
  const requests = query => result.requests.filter(item => item.path === '/api/gateway/search' && item.query === query);
  const receipts = query => result.receipts.filter(item => item.query === query);
  const failureRow = text => quick.locator('.monaco-list-row').filter({ hasText: text });
  if (scenario === 'identity') {
    // TODO（HTTP/1.1 长流饥饿）：主窗口插件、观察和导航长流占满同源六连接时，
    // 独立 Search Editor 与 % 重试的并发组合无法分发；原 owner 解决后恢复该组合。
    // 两次失败原件保留于 run-4ff00d2cf07b 和 run-610116431e6d，不算身份组合验收通过。
    result.identitySchedule = '两个顺序阶段分别通过原产品命令的401撤销，不证明同页并发组合';
    result.pendingIdentityReads = {};
    const selectBoth = async () => {
      await command('workbench.action.quickOpen', '');
      await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
      for (const owner of [a, b]) await quick.locator('.monaco-list-row').filter({ hasText: owner.workspace_id }).getByRole('checkbox').check();
      await quick.getByRole('button', { name: '确定', exact: true }).click();
    };
    const assertPending = async query => {
      await expect.poll(async () => (await searchFault(page, query)).reads.some(read => read.received)).toBe(true);
      const observations = (await searchFault(page, query)).reads;
      expect(observations).toHaveLength(1); expect(observations[0].received).toBe(true);
      expect(observations[0].cancelled).toBe(false); expect(observations[0].settled).toBe(false);
      expect(observations[0].workspace_id).toBe(a.workspace_id);
      await expect.poll(() => result.searchResponses.findLast(item => item.query === query)?.requestId).toBe(observations[0].request_id);
      result.pendingIdentityReads[query] = observations;
    };
    const revokeIdentity = async phase => {
      let injected = false;
      const requestId = `req-text-identity-${phase}`;
      const expired = async route => {
        if (injected || new URL(route.request().url()).search !== '?check_health=false') { await route.continue(); return; }
        expect(route.request().headers()['x-local-token']).toBeTruthy(); injected = true;
        await route.fulfill({ status: 401, contentType: 'application/json', headers: { 'X-Request-ID': requestId },
          body: JSON.stringify({ request_id: requestId, detail: 'invalid local token' }) });
      };
      const pattern = '**/api/gateway/workspaces?check_health=false', writes = [];
      const observeWrite = request => { if (/\/api\/gateway\/workspaces\/[^/]+\/activate$/.test(new URL(request.url()).pathname)) writes.push(request.url()); };
      page.on('request', observeWrite); await page.route(pattern, expired);
      try {
        // 原命令先读取登记；requestJson 消费具名401广播身份失效，命令自己的守卫阻止后续激活。
        const outcome = await command('boxteam.workspace.activate', { gatewayId: a.gateway_id, workspaceId: a.workspace_id })
          .then(() => ({ resolved: true }), error => ({ error: String(error) }));
        (result.invalidatedCommands ??= []).push({ phase, requestId, outcome, writes });
        expect(injected).toBe(true); expect(outcome.error).toContain('Gateway 或访问凭据已改变');
        expect(writes).toEqual([]);
      } finally { await page.unroute(pattern, expired); page.off('request', observeWrite); }
    };
    await page.keyboard.press('Escape');
    await command('workbench.action.closeAllEditors');
    await expect(page.locator('.boxteam-thread')).toHaveCount(0);
    await command('workbench.action.files.newUntitledFile');
    result.phase = 'retry-in-flight-before-identity-failure';
    await searchFault(page, 'identityretry', 'fail', ['file'], [a.workspace_id]);
    await selectBoth(); await input.fill('%identityretry');
    await expect(quick).toContainText('identityretry B original'); await idle();
    await expect(quick).toContainText('文件搜索失败：');
    await command('workbench.action.keepEditor');
    await searchFault(page, 'identityretry', 'delay', ['file'], [a.workspace_id]);
    await failureRow('文件搜索失败：').hover();
    await failureRow('文件搜索失败：').getByRole('button', { name: '重新查询此文件分支', exact: true }).click();
    await assertPending('identityretry');
    result.phase = 'product-http-401-revokes-percent-retry';
    await revokeIdentity('retry');
    await expect(quick).toContainText('Gateway 或访问凭据已改变，请重新打开快速访问'); await idle();
    await expect(quick).not.toContainText('identityretry B original');
    await expect(quick.getByRole('button', { name: '重新查询此文件分支', exact: true })).toHaveCount(0);
    await expect.poll(async () => (await searchFault(page, 'identityretry')).reads.some(read => read.cancelled && read.settled)).toBe(true);
    result.identityCancelsRetry = true;
    const before = result.requests.length;
    await input.fill('%identityafter'); await input.press('Enter');
    await searchFault(page, 'identityretry', 'release', ['file']);
    await page.waitForTimeout(100);
    expect(result.requests).toHaveLength(before); await expect(quick).toContainText('请重新打开快速访问');
    result.identityRevokesCandidates = true;
    result.identityReads = { retry: (await searchFault(page, 'identityretry')).reads };
    await page.keyboard.press('Escape');
    result.phase = 'independent-search-editor-in-flight';
    await command('workbench.action.closeAllEditors');
    await command('workbench.action.files.newUntitledFile');
    await searchFault(page, 'identityeditor', 'delay', ['file'], [a.workspace_id]);
    await command('search.action.openEditor', { query: 'identityeditor', location: 'new', triggerSearch: true,
      useExcludeSettingsAndIgnoreFiles: false, contextLines: 0, onlyOpenEditors: false });
    const editor = page.locator('.search-editor');
    await expect(editor).toHaveCount(1);
    const editorScope = await editor.locator('.boxteam-search-scope').innerText();
    await assertPending('identityeditor');
    result.phase = 'product-http-401-revokes-search-editor';
    await revokeIdentity('editor');
    await expect.poll(async () => (await searchFault(page, 'identityeditor')).reads.some(read => read.cancelled && read.settled)).toBe(true);
    result.identityCancelsSearchEditor = true;
    result.identityReads.editor = (await searchFault(page, 'identityeditor')).reads;
    await searchFault(page, 'identityeditor', 'release', ['file']);
    result.phase = 'search-editor-new-query-keeps-original-scope';
    await expect(editor).toHaveCount(1);
    await expect(editor.locator('.boxteam-search-scope')).toHaveText(editorScope);
    const editorQuery = editor.getByRole('textbox', { name: '搜索: 键入搜索词，然后按 Enter 进行搜索', exact: true });
    await editorQuery.fill('identityafter'); await editorQuery.press('Enter');
    await expect(editor.locator('.monaco-editor .view-lines')).toContainText('identityafter A original');
    await expect(editor.locator('.monaco-editor .view-lines')).not.toContainText('identityafter B original');
    expect(requests('identityafter')).toHaveLength(1);
    expect(requests('identityafter')[0].owners).toEqual([a.workspace_id]); result.newQueryUsesFrozenEditorScope = true;
    result.phase = 'reopen-percent-with-new-identity';
    await selectBoth(); await input.fill('%identityafter');
    await expect(quick).toContainText('identityafter A original'); await expect(quick).toContainText('identityafter B original'); await idle();
    expect(requests('identityafter')).toHaveLength(2);
    expect(requests('identityafter')[1].owners).toEqual([a.workspace_id, b.workspace_id]); result.reopenUsesNewIdentity = true;
    await page.keyboard.press('Escape');
  } else {
  result.phase = 'failed-keeps-successful-sibling';
  await searchFault(page, 'textrecovery', 'fail', ['file'], [a.workspace_id]);
  await input.fill('%textrecovery');
  await expect(quick).toContainText('textrecovery B original'); await idle();
  await expect(quick).toContainText('文件搜索失败：');
  await expect(quick).toContainText('code: owner_http_error'); await expect(quick).toContainText('retryable: true');
  await expect(quick).not.toContainText('http_status:');
  await expect.poll(() => receipts('textrecovery').length).toBe(1);
  const failed = receipts('textrecovery')[0], failedBranch = failed.summary.branches.find(state => state.state === 'failed');
  expect(failedBranch.branch.owner).toEqual(a); expect(failedBranch.code).toBe('owner_http_error');
  expect(failed.summary.branches.find(state => state.branch.owner.workspace_id === b.workspace_id).state).toBe('complete');
  await expect(quick).toContainText(`request_id: ${failed.requestId}`);
  await expect(quick).toContainText(a.workspace_id); result.failedKeepsSibling = true;
  await searchFault(page, 'textrecovery', 'release', ['file']);
  await failureRow('文件搜索失败：').hover();
  await failureRow('文件搜索失败：').getByRole('button', { name: '重新查询此文件分支', exact: true }).click();
  await expect(quick).toContainText('textrecovery A original'); await expect(quick).toContainText('textrecovery B original'); await idle();
  await expect(quick).not.toContainText('文件搜索失败：');
  await expect.poll(() => receipts('textrecovery').length).toBe(2);
  const [first, retry] = requests('textrecovery');
  expect(requests('textrecovery')).toHaveLength(2);
  expect(retry.body.scope).toEqual({ mode: 'resume', scope: failed.scope, owners: [] });
  expect(retry.body.continuations).toEqual([{ branch: failedBranch.branch, cursor: failedBranch.cursor }]);
  expect(retry.body.queries[0].limit).toBe(first.body.queries[0].limit);
  expect(retry.body.queries[0].file_targets).toEqual(first.body.queries[0].file_targets.filter(target => target.owner.workspace_id === a.workspace_id));
  expect(receipts('textrecovery')[1].summary.branches).toHaveLength(1);
  result.recoveryReads = (await searchFault(page, 'textrecovery')).reads;
  expect(result.recoveryReads.map(read => read.workspace_id).sort()).toEqual([a.workspace_id, a.workspace_id, b.workspace_id].sort());
  result.retryOnlyFailedBranch = true;
  // 沿原保留动作固定成功候选预览，下一查询不因替换该模型而正确拒绝旧重试。
  await command('workbench.action.keepEditor');
  result.phase = 'remote-cancel-keeps-successful-sibling';
  await searchFault(page, 'textcancel', 'cancel', ['file'], [a.workspace_id]);
  await input.fill('%textcancel');
  await expect(quick).toContainText('textcancel B original'); await idle();
  await expect(quick).toContainText('文件搜索已取消：');
  await expect(quick).not.toContainText('文件搜索失败：');
  await expect(quick).toContainText('code: cancelled'); await expect(quick).toContainText('retryable: true');
  await expect.poll(() => receipts('textcancel').length).toBe(1);
  const cancelled = receipts('textcancel')[0];
  expect(cancelled.summary.branches.find(state => state.branch.owner.workspace_id === a.workspace_id).state).toBe('cancelled');
  await expect(quick).toContainText(`request_id: ${cancelled.requestId}`); result.cancelledKeepsSibling = true;
  await searchFault(page, 'textcancel', 'delay', ['file'], [a.workspace_id]);
  await failureRow('文件搜索已取消：').hover();
  await failureRow('文件搜索已取消：').getByRole('button', { name: '重新查询此文件分支', exact: true }).click();
  await expect.poll(async () => (await searchFault(page, 'textcancel')).reads.some(read => read.received && read.workspace_id === a.workspace_id)).toBe(true);
  await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(1);
  await page.keyboard.press('Escape'); await expect(quick).not.toBeVisible();
  await expect.poll(async () => (await searchFault(page, 'textcancel')).reads.some(read => read.cancelled && read.settled)).toBe(true);
  await searchFault(page, 'textcancel', 'release', ['file']);
  result.cancelledReads = (await searchFault(page, 'textcancel')).reads;
  expect(result.cancelledReads).toHaveLength(1); expect(result.cancelledReads[0].workspace_id).toBe(a.workspace_id);
  result.closedRetryAbortsRead = true;
  }
  await Promise.all(reads); expect(readErrors).toEqual([]); expect(result.pageErrors).toEqual([]); expect(result.modelCalls).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error.stack ?? String(error);
  if (page && scenario === 'identity') result.failedIdentityReads = await Promise.all(['identityretry', 'identityeditor']
    .map(query => searchFault(page, query).then(observation => ({ query, ...observation }), error => ({ query, error: String(error) }))));
  if (page) await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
} finally { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); await run.close(); }
if (failure) throw failure;
