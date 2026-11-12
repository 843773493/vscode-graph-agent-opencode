import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { searchReceipt } from '../../../support/workbench/search-observation.mjs';
import { scrollQuickInput } from '../../../support/workbench/quick-input.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!base || !output) throw new Error('首次文件快速访问验收缺少 Gateway 或产物路径');
const run = TestRunContext.fromTestFile('tests/e2e/clients/web/test_quick_access_file_branch.py');
const result = { classification: 'E2E', phase: 'initialize', pageErrors: [], apiReceipts: [], roots: [], searches: [],
  boundary: '正式 Workbench、真实 Gateway／工作区后端；首次单次默认 Quick Open，原 5 秒窗口，无模型执行' };
const reads = [], readErrors = [], requests = new Map();
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path !== '/api/v1/workspace/files/roots' && path !== '/api/gateway/search') return;
    const observed = { path, startedAt: Date.now(), phase: result.phase, state: 'pending',
      owner: { gateway_id: request.headers()['x-boxteam-target-gateway-id'], workspace_id: request.headers()['x-boxteam-workspace-id'] },
      body: request.method() === 'POST' ? request.postDataJSON() : undefined };
    requests.set(request, observed);
    (path.endsWith('/roots') ? result.roots : result.searches).push(observed);
  });
  page.on('requestfinished', request => {
    const observed = requests.get(request);
    if (!observed) return;
    observed.state = 'finished'; observed.finishedAt = Date.now();
    reads.push((async () => {
      if (observed.path === '/api/gateway/search') observed.receipt = await searchReceipt(request);
      else {
        const response = await request.response();
        observed.response = { status: response.status(), headers: response.headers(), body: await response.json() };
        expect(observed.response.body.request_id).toBe(observed.response.headers['x-request-id']);
      }
    })().catch(error => readErrors.push(String(error))));
  });
  page.on('requestfailed', request => {
    const observed = requests.get(request);
    if (observed) { observed.state = 'failed'; observed.finishedAt = Date.now(); observed.failure = request.failure(); }
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const api = async (path, owner, data) => {
    const response = await page.evaluate(async ({ path, owner, data }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
        headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
          'X-BoxTeam-Workspace-Id': owner.workspace_id, 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id,
        } : {}) }, body: data === undefined ? undefined : JSON.stringify(data) });
      const body = await response.json(), header = response.headers.get('X-Request-ID');
      if (!response.ok || !body.request_id || body.request_id !== header) throw new Error(JSON.stringify({ path, status: response.status, body }));
      return { data: body.data, receipt: { path, owner, status: response.status, requestId: body.request_id, header } };
    }, { path, owner, data });
    result.apiReceipts.push(response.receipt);
    return response.data;
  };
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces?check_health=false');
  const owner = targets.active_workspace;
  expect(owner).toEqual({ gateway_id: health.gateway_id, workspace_id: expect.any(String) });
  expect((await api('/api/v1/workspace', owner)).workspace_id).toBe(owner.workspace_id);
  result.owner = owner;
  const session = await api('/api/v1/sessions', owner, { title: '首次文件快速访问验收' });
  result.sessionId = session.session_id;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('treeitem', { name: /^首次文件快速访问验收/ })).toBeVisible();
  result.phase = 'first-default-quick-open';
  result.queryStartedAt = Date.now();
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.quickOpen', '定位😀词'));
  const quick = page.locator('.quick-input-widget');
  await expect(quick).toBeVisible();
  await expect.poll(async () => {
    await scrollQuickInput(page, quick, true);
    return quick.getByRole('listbox').innerText();
  }, { timeout: 5_000 }).toContain('文件 · 没有匹配的资源');
  result.queryCompletedAt = Date.now();
  result.fileBranchCompleted = true;
  await Promise.all(reads);
  expect(readErrors).toEqual([]);
  const roots = result.roots.filter(item => item.phase === result.phase);
  expect(roots).toHaveLength(1);
  expect(roots[0].owner).toEqual(owner);
  expect(roots[0].response.status).toBe(200);
  expect(roots[0].response.body.data.workspace_uri).toEqual(expect.any(String));
  expect(result.searches).toHaveLength(2);
  const nameSearch = result.searches[0];
  expect(nameSearch.body.queries.map(query => query.category)).toEqual(['catalog', 'thread']);
  expect(nameSearch.receipt.scope.owners).toEqual([owner]);
  expect(nameSearch.receipt.summary.state).toBe('complete');
  const fileSearch = result.searches.find(item => item.body.queries.some(query => query.category === 'file'));
  const fileRequest = [...requests].find(([, observed]) => observed === fileSearch)[0];
  fileSearch.receipt = await searchReceipt(fileRequest);
  expect(fileSearch.body.queries).toHaveLength(1);
  expect(fileSearch.body.queries[0].file_targets).toHaveLength(1);
  expect(fileSearch.body.queries[0].file_targets[0].owner).toEqual(owner);
  expect(fileSearch.body.queries[0].file_targets[0].target.uri).toBe(roots[0].response.body.data.workspace_uri);
  expect(fileSearch.receipt.scope.owners).toEqual([owner]);
  expect(fileSearch.receipt.summary.branches[0].state).toBe('complete');
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error);
  if (page) await page.screenshot({ path: output.replace(/\.json$/, '-failure.png'), fullPage: true });
} finally {
  if (page && !page.isClosed()) {
    result.quickInput = await page.locator('.quick-input-widget [data-quick-input-id]').allTextContents();
  }
  await Promise.all(reads);
  result.readErrors = readErrors;
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
