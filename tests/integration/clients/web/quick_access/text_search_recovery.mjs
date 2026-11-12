import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { searchRequest, searchReceipt } from '../../../../support/workbench/search-observation.mjs';
import { searchFault } from './query_faults.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!base || !output || !process.env.BOXTEAM_RECOVERY_WORKSPACE_B) throw new Error('文本分支恢复缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_text_search_recovery.py');
const result = { phase: 'initialize', pageErrors: [], requests: [], receipts: [], initialization: [], modelCalls: [],
  boundary: '真实Gateway/Workspace文件owner与正式Workbench；仅在Gateway出站边界按精确workspace注入失败、取消或暂缓真实响应，不替换搜索调度、匹配、SSE或客户端状态' };
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
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
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
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
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
  await Promise.all(reads); expect(readErrors).toEqual([]); expect(result.pageErrors).toEqual([]); expect(result.modelCalls).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error.stack ?? String(error);
  if (page) await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
} finally { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); await run.close(); }
if (failure) throw failure;
