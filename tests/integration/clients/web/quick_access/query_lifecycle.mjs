import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { searchRequest, searchReceipt } from '../../../../support/workbench/search-observation.mjs';
import { searchFault } from './query_faults.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!base || !output || !process.env.BOXTEAM_QUERY_WORKSPACE_B || !process.env.BOXTEAM_QUERY_WORKSPACE_C) throw new Error('查询验收缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_query_lifecycle.py');
const result = { classification: 'Integration', phase: 'prepare', requests: [], receipts: [], cancelled: [], pageErrors: [], forbiddenWrites: [],
  boundary: '正式 Workbench、真实 Gateway 和隔离 A/B/C；Session 由产品 API 创建；指定 Gateway 出站分支注入失败或延迟真实响应；401 只在浏览器 HTTP 边界注入，不替代匹配／调度／SSE 或业务状态' };
let page, failure;
const responseReads = [], responseErrors = [];
let truncatedRequest;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1400, height: 1200 } });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const observed = searchRequest(request);
    if (observed) {
      const headers = request.headers();
      result.requests.push({ ...observed, targetOwner: headers['x-boxteam-workspace-id'] ? {
        gateway_id: headers['x-boxteam-target-gateway-id'], workspace_id: headers['x-boxteam-workspace-id'],
      } : undefined });
    }
    if (request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(new URL(request.url()).pathname)) result.forbiddenWrites.push(request.url());
  });
  page.on('requestfailed', request => {
    const observed = searchRequest(request);
    if (observed) result.cancelled.push({ path: observed.path, query: observed.query, categories: observed.categories, error: request.failure()?.errorText });
  });
  page.on('requestfinished', request => {
    if (!searchRequest(request) || request === truncatedRequest) return;
    responseReads.push(searchReceipt(request).then(receipt => result.receipts.push(receipt)).catch(error => responseErrors.push(String(error))));
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const api = async (path, owner, data) => page.evaluate(async ({ path, owner, data }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST', body: data === undefined ? undefined : JSON.stringify(data),
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Workspace-Id': owner.workspace_id, 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id,
      } : {}) } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return body.data;
  }, { path, owner, data });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const owner = ({ gateway_id, workspace_id }) => ({ gateway_id, workspace_id });
  const sameOwner = (left, right) => left.gateway_id === right.gateway_id && left.workspace_id === right.workspace_id;
  const health = await api('/api/gateway/health'), initial = await api('/api/gateway/workspaces?check_health=false');
  const ownerA = initial.active_workspace;
  expect(ownerA.gateway_id).toBe(health.gateway_id);
  const register = async (path, name) => {
    await api('/api/gateway/managed-workspaces', undefined, { root_path: path, name });
    const target = (await api('/api/gateway/workspaces?check_health=false')).items.find(item => item.name === name);
    if (!target?.gateway_id || !target.workspace_id) throw new Error(`注册后缺少精确工作区身份：${name}`);
    return target;
  };
  const b = await register(process.env.BOXTEAM_QUERY_WORKSPACE_B, '查询目标 B');
  const c = await register(process.env.BOXTEAM_QUERY_WORKSPACE_C, '查询目标 C');
  await api(`/api/gateway/workspaces/${c.workspace_id}/runtime/stop`, owner(c), {});
  const a = initial.items.find(item => sameOwner(item, ownerA)), owners = [a, b];
  for (const target of owners) {
    for (const title of ['branchscope', 'slowcancel', 'allfailure', 'streamdiagnostic']) await api('/api/v1/sessions', owner(target), { title: `${title} ${target === a ? 'A' : 'B'}` });
  }
  // 经过真实 Gateway 验证总期限，不以浏览器取消或故障替身冒充服务端拒绝。
  result.deadlineRejection = await page.evaluate(async owner => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const receipts = [];
    for (const path of ['/api/v1/session-catalog/search', '/api/v1/session-threads/search']) {
      const response = await fetch(`${path}?query=budgetcontract`, { headers: {
        'X-Local-Token': credential.data.token, 'X-BoxTeam-Workspace-Id': owner.workspace_id,
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id,
        'X-BoxTeam-Name-Search-Deadline': '1',
      } });
      receipts.push({ path, status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() });
    }
    return receipts;
  }, ownerA);
  for (const receipt of result.deadlineRejection) {
    expect(receipt.status).toBe(409);
    expect(receipt.body.request_id).toBe(receipt.header);
    expect(JSON.stringify(receipt.body)).toContain('总执行期限');
  }
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const key = target => `${target.gateway_id}/${target.workspace_id}`;
  const status = (target, category) => quick.locator(`[data-quick-input-id="status-${key(target)}/${category}"]`);
  const showStatus = async (target, category) => {
    // 原列表会随渐进结果重新虚拟化；每次观察都通过原导航到对应区域。
    await command(category === '文件' ? 'quickInput.last' : 'quickInput.first');
    return status(target, category);
  };
  const expectStatus = async (target, category, text, options) => {
    await expect.poll(async () => (await showStatus(target, category)).evaluateAll(nodes => nodes[0]?.textContent ?? ''), options).toContain(text);
  };
  const queries = query => result.requests.filter(item => item.query === query);
  const requestOwners = item => item.body.scope.mode === 'resume' ? item.body.scope.scope.owners : item.body.scope.owners;
  const inactive = () => expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
  const choose = async targets => {
    await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
    await expect(quick).toContainText('选择快速访问搜索范围');
    for (const target of targets) await quick.locator('.monaco-list-row').filter({ hasText: target.gateway_id })
      .filter({ hasText: target.workspace_id }).getByRole('checkbox').check();
    await quick.getByRole('button', { name: 'OK', exact: true }).click();
  };
  await command('workbench.action.quickOpen', 'branchscope');
  await expectStatus(a, '文件', '本次查询完成');
  expect(queries('branchscope').filter(item => item.path === '/api/gateway/search' && !item.categories.includes('file')).map(requestOwners)).toEqual([[owner(a)]]);
  expect(queries('branchscope').filter(item => item.categories?.includes('file')).map(requestOwners)).toEqual([[owner(a)]]);
  await choose([b]);
  await expectStatus(b, '文件', '本次查询完成');
  await expectStatus(b, 'Thread', '本次查询完成');
  await inactive();
  expect(queries('branchscope').filter(item => item.path === '/api/gateway/search' && !item.categories.includes('file')).map(requestOwners)).toEqual([[owner(a)], owners.map(owner)]);
  expect(queries('branchscope').filter(item => item.categories?.includes('file')).map(requestOwners)).toEqual([[owner(a)], owners.map(owner)]);
  expect(new Set(result.requests.filter(item => item.path === '/api/v1/workspace/files/roots').map(item => JSON.stringify(item.targetOwner))))
    .toEqual(new Set(owners.map(target => JSON.stringify(owner(target)))));
  expect(queries('branchscope').filter(item => item.query).every(item => Number(item.deadline) > item.issuedAt && Number(item.deadline) <= item.issuedAt + 15)).toBe(true);
  result.serverBudget = true;
  for (const target of owners) for (const category of ['会话', 'Thread', '文件']) {
    await expectStatus(target, category, 'request_id:');
    await expectStatus(target, category, '不代表全局快照');
  }
  result.onlySelectedOwners = true;

  result.phase = 'disconnect-before-scope';
  const searchPattern = '**/api/gateway/search';
  const truncateBeforeScope = async route => {
    const observed = searchRequest(route.request());
    if (truncatedRequest || observed.query !== 'streamdiagnostic' || observed.categories.includes('file')) return route.continue();
    truncatedRequest = route.request();
    // 明确的浏览器 HTTP 故障：原请求仍经真实 Gateway 完成，仅交付空正文，保留原响应头。
    const response = await route.fetch(), original = await response.body();
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toContain('text/event-stream');
    const requestId = response.headers()['x-request-id'];
    expect(requestId).toBeTruthy(); expect(original.length).toBeGreaterThan(0);
    result.disconnectedResponse = { requestId, headers: response.headers(), status: response.status(), originalBytes: original.length, deliveredBytes: 0,
      query: observed.body, boundary: '仅原浏览器HTTP响应正文被截为空；原Gateway响应头及请求身份保留' };
    await route.fulfill({ response, body: '' });
  };
  await page.route(searchPattern, truncateBeforeScope);
  try {
    await input.fill('streamdiagnostic');
    for (const target of owners) for (const category of ['会话', 'Thread']) {
      await expectStatus(target, category, '未收到完整终态');
      await expectStatus(target, category, result.disconnectedResponse.requestId);
    }
    await expectStatus(b, '文件', '本次查询完成'); await inactive();
    const names = queries('streamdiagnostic').filter(item => !item.categories.includes('file'));
    expect(names).toHaveLength(1);
    const fileReceipts = () => result.receipts.filter(item => item.query === 'streamdiagnostic'
      && item.summary.branches.every(state => state.branch.category === 'file'));
    await expect.poll(() => fileReceipts().length).toBe(1);
    const fileReceipt = fileReceipts()[0];
    expect(fileReceipt.scope.owners).toEqual(names[0].body.scope.owners);
    result.disconnectedResponse.scopeSource = { requestId: fileReceipt.requestId, header: fileReceipt.header, scope: fileReceipt.scope };
    await (await showStatus(a, 'Thread')).hover();
    await status(a, 'Thread').getByRole('button', { name: '重新查询此类别', exact: true }).click();
    await expectStatus(a, 'Thread', '本次查询完成');
    for (const target of owners) for (const category of ['会话', 'Thread']) {
      if (sameOwner(target, a) && category === 'Thread') continue;
      await expectStatus(target, category, '未收到完整终态');
      await expectStatus(target, category, result.disconnectedResponse.requestId);
    }
    await expectStatus(b, '文件', '本次查询完成');
    await inactive();
    const repeated = queries('streamdiagnostic').filter(item => !item.categories.includes('file'));
    expect(repeated).toHaveLength(2);
    expect(repeated[1].body.scope).toEqual({ mode: 'resume', scope: fileReceipt.scope, owners: [] });
    expect(repeated[1].body.queries).toEqual(repeated[0].body.queries.filter(query => query.category === 'thread'));
    expect(repeated[1].body.continuations).toEqual([{ branch: { owner: owner(a), category: 'thread', operation: 'name' } }]);
    result.disconnectedRetry = { scopeSourceRequestId: fileReceipt.requestId, query: repeated[1].body };
    result.disconnectPreservesRequestIdentity = true;
  } finally { await page.unroute(searchPattern, truncateBeforeScope); }

  result.phase = 'cancel-gateway-queue';
  await searchFault(page, 'slowcancel', 'delay');
  await input.fill('slowcancel');
  await expect.poll(async () => (await searchFault(page, 'slowcancel')).reads.filter(read => read.received).length).toBe(3);
  expect(queries('slowcancel').filter(item => item.path === '/api/gateway/search' && !item.categories.includes('file'))).toHaveLength(1);
  await input.fill('branchscope');
  await expectStatus(b, '文件', '本次查询完成');
  await inactive();
  await expect.poll(async () => (await searchFault(page, 'slowcancel')).reads.filter(read => read.cancelled && read.settled).length).toBe(3);
  result.cancelledReads = (await searchFault(page, 'slowcancel')).reads;
  expect(result.cancelledReads).toHaveLength(3); // 第四个名称分支未越过 Gateway 队列。
  await searchFault(page, 'slowcancel', 'release');
  await expect.poll(() => result.cancelled.filter(item => item.path === '/api/gateway/search' && item.query === 'slowcancel' && !item.categories.includes('file')).length).toBe(1);
  await expect(quick).not.toContainText('slowcancel');
  result.cancelledQueueAndLateResponses = true;

  result.phase = 'real-timeout-and-other-branches';
  await searchFault(page, 'branchscope', 'delay', ['catalog']);
  try {
    const before = queries('branchscope').length;
    await input.fill('branchscope');
    await page.waitForTimeout(250);
    expect(queries('branchscope').length).toBe(before);
    await input.fill('branchscope A');
    await expectStatus(a, '文件', '没有匹配的资源');
    await input.fill('branchscope');
    await expect.poll(async () => (await searchFault(page, 'branchscope')).reads.filter(read => read.category === 'catalog' && read.received).length).toBe(2);
    await expectStatus(b, 'Thread', '本次查询完成');
    await expectStatus(a, '文件', '本次查询完成');
    // 文件已结束，但两个 catalog 仍等待，不能提前隐藏总 busy。
    await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(1);
    await expectStatus(a, '会话', '查询超时', { timeout: 18_000 });
    await expectStatus(b, '会话', '查询超时');
    await expectStatus(b, '文件', '本次查询完成');
    await inactive();
    await expect.poll(async () => (await searchFault(page, 'branchscope')).reads.filter(read => read.category === 'catalog' && read.cancelled).length).toBe(2);
    result.timeoutReads = (await searchFault(page, 'branchscope')).reads;
  } finally { await searchFault(page, 'branchscope', 'release'); }
  await expectStatus(a, '会话', '查询超时');
  result.timeoutKeepsOtherBranches = true;

  result.phase = 'all-failed-and-single-branch-retry';
  await searchFault(page, 'allfailure', 'fail', ['catalog', 'thread', 'file']);
  try {
    await input.fill('allfailure');
    for (const target of owners) for (const category of ['会话', 'Thread', '文件']) {
      await expectStatus(target, category, '查询失败');
      await expectStatus(target, category, 'request_id:');
    }
    await inactive();
    result.failedReads = (await searchFault(page, 'allfailure')).reads;
    expect(result.failedReads).toHaveLength(6);
    expect(new Set(result.failedReads.filter(read => read.category !== 'file').map(read => read.request_id)).size).toBe(1);
    expect(new Set(result.failedReads.filter(read => read.category === 'file').map(read => read.request_id)).size).toBe(1);
    for (const target of owners) for (const category of ['会话', 'Thread']) await expectStatus(target, category, result.failedReads.find(read => read.category === 'catalog').request_id);
    await expect(quick).not.toContainText('没有匹配的资源');
    result.allFailuresRemainFailures = true;
  } finally { await searchFault(page, 'allfailure', 'release'); }
  const beforeRetry = result.requests.length;
  await (await showStatus(a, 'Thread')).hover();
  await status(a, 'Thread').getByRole('button', { name: '重新查询此类别', exact: true }).click();
  await expectStatus(a, 'Thread', '本次查询完成');
  await expectStatus(b, 'Thread', '查询失败');
  await expectStatus(b, 'Thread', result.failedReads.find(read => read.category === 'catalog').request_id);
  await inactive();
  const retried = result.requests.slice(beforeRetry);
  expect(retried).toHaveLength(1);
  expect(retried[0].path).toBe('/api/gateway/search');
  expect(retried[0].query).toBe('allfailure');
  expect(retried[0].body.scope.mode).toBe('resume');
  expect(retried[0].body.continuations).toEqual([{ branch: { owner: owner(a), category: 'thread', operation: 'name' } }]);
  expect(Number(retried[0].deadline)).toBeGreaterThan(retried[0].issuedAt);
  expect(Number(retried[0].deadline)).toBeLessThanOrEqual(retried[0].issuedAt + 15);
  result.retriedReads = (await searchFault(page, 'allfailure')).reads;
  expect(result.retriedReads).toHaveLength(7);
  expect(result.retriedReads.at(-1).workspace_id).toBe(a.workspace_id);
  expect(result.retriedReads.at(-1).category).toBe('thread');
  expect(result.retriedReads.at(-1).request_id).not.toBe(result.failedReads.find(read => read.category === 'catalog').request_id);
  result.retryOnlyFailedBranch = true;

  result.phase = 'offline-scope';
  await choose([c]);
  await expectStatus(c, '会话', '查询失败');
  await inactive();
  expect(result.requests.every(item => !item.targetOwner || !sameOwner(item.targetOwner, c))).toBe(true);
  expect((await searchFault(page, 'allfailure')).reads.every(read => read.workspace_id !== c.workspace_id)).toBe(true);
  result.offlineHasNoBusinessQuery = true;

  result.phase = 'identity-recovery-revokes-picks';
  const expire = async route => {
    if (searchRequest(route.request())?.query !== 'identityfail') { await route.continue(); return; }
    await route.fulfill({ status: 401, contentType: 'application/json', headers: { 'X-Request-ID': 'req-injected-identity' },
      body: JSON.stringify({ request_id: 'req-injected-identity', detail: { code: 'user_session_required' } }) });
  };
  const identityPattern = '**/api/gateway/search';
  await page.route(identityPattern, expire);
  try {
    await input.fill('identityfail');
    await expect(quick).toContainText('Gateway 或用户身份已改变，请重新打开快速访问');
    await inactive();
    const before = result.requests.length;
    await input.fill('branchscope');
    await page.waitForTimeout(300);
    expect(result.requests).toHaveLength(before);
    expect(await quick.locator('[data-quick-input-id^="boxteam-"]').count()).toBe(0);
    result.identityRevokesCandidates = true;
  } finally { await page.unroute(identityPattern, expire); }
  await page.keyboard.press('Escape');
  await Promise.all(responseReads);
  expect(responseErrors).toEqual([]);
  expect(result.requests.filter(item => ['/api/v1/session-catalog/search', '/api/v1/session-threads/search', '/api/v1/resources/navigation'].includes(item.path) && item.query !== 'budgetcontract')).toEqual([]);
  expect(result.forbiddenWrites).toEqual([]);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) { failure = error; }
finally {
  try { if (failure && page) await page.screenshot({ path: output.replace(/\.json$/, '.failure.png'), fullPage: true }); }
  catch (error) { failure = new AggregateError([failure, error], '场景与截图均失败'); }
  if (failure) result.error = String(failure.stack ?? failure);
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
console.log(JSON.stringify({ phase: result.phase, output }));
