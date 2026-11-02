import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!base || !output || !process.env.BOXTEAM_QUERY_WORKSPACE_B || !process.env.BOXTEAM_QUERY_WORKSPACE_C) throw new Error('查询验收缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_query_lifecycle.py');
const result = { classification: 'Integration', phase: 'prepare', requests: [], receipts: [], cancelled: [], pageErrors: [], forbiddenWrites: [],
  boundary: '正式 Workbench、真实 Gateway 和隔离 A/B/C；Session 由产品 API 创建；显式注入 503、401 和延迟真实响应，不替代模型或业务状态' };
let page, failure;
const responseReads = [], responseErrors = [];
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1400, height: 1200 } });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  const isSearch = request => ['/api/v1/session-catalog/search', '/api/v1/session-threads/search'].includes(new URL(request.url()).pathname)
    || new URL(request.url()).pathname === '/api/v1/resources/navigation' && request.postDataJSON()?.operation === 'search';
  const queryOf = request => new URL(request.url()).searchParams.get('query') ?? request.postDataJSON()?.query;
  page.on('request', request => {
    if (isSearch(request)) result.requests.push({ path: new URL(request.url()).pathname, query: queryOf(request),
      route: request.headers()['x-boxteam-workspace-id'], deadline: request.headers()['x-boxteam-resource-navigation-deadline'] });
    if (request.method() !== 'GET' && /\/(messages|jobs)(\/|$)/.test(new URL(request.url()).pathname)) result.forbiddenWrites.push(request.url());
  });
  page.on('requestfailed', request => {
    if (isSearch(request)) result.cancelled.push({ query: queryOf(request), error: request.failure()?.errorText });
  });
  page.on('requestfinished', request => {
    if (!isSearch(request)) return;
    responseReads.push((async () => {
      const response = await request.response(), body = await response.json();
      expect(body.request_id).toBeTruthy();
      expect(body.request_id).toBe(response.headers()['x-request-id']);
      result.receipts.push({ path: new URL(request.url()).pathname, query: queryOf(request), status: response.status(), requestId: body.request_id });
    })().catch(error => responseErrors.push(String(error))));
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const api = async (path, route, data) => page.evaluate(async ({ path, route, data }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST', body: data === undefined ? undefined : JSON.stringify(data),
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(route ? { 'X-BoxTeam-Workspace-Id': route } : {}) } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return body.data;
  }, { path, route, data });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), initial = await api('/api/gateway/workspaces?check_health=false');
  const routeA = initial.active_workspace_id;
  const register = async (path, name) => {
    await api('/api/gateway/managed-workspaces', undefined, { root_path: path, name });
    const target = (await api('/api/gateway/workspaces?check_health=false')).items.find(item => item.name === name);
    if (!target?.backend_workspace_id) throw new Error(`注册后缺少精确工作区身份：${name}`);
    return target;
  };
  const b = await register(process.env.BOXTEAM_QUERY_WORKSPACE_B, '查询目标 B');
  const c = await register(process.env.BOXTEAM_QUERY_WORKSPACE_C, '查询目标 C');
  await api(`/api/gateway/workspaces/${c.workspace_id}/runtime/stop`, undefined, {});
  const a = initial.items.find(item => item.workspace_id === routeA), owners = [a, b];
  for (const target of owners) {
    for (const title of ['branchscope', 'slowcancel', 'allfailure']) await api('/api/v1/sessions', target.workspace_id, { title: `${title} ${target === a ? 'A' : 'B'}` });
  }
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const key = target => `${health.gateway_id}/${target.backend_workspace_id}`;
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
  const inactive = () => expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
  const choose = async targets => {
    await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
    await expect(quick).toContainText('选择快速访问搜索范围');
    for (const target of targets) await quick.locator('.monaco-list-row').filter({ hasText: target.backend_workspace_id }).getByRole('checkbox').check();
    await quick.getByRole('button', { name: 'OK', exact: true }).click();
  };
  await command('workbench.action.quickOpen', 'branchscope');
  await expectStatus(a, '文件', '本次查询完成');
  expect(queries('branchscope').every(item => item.route === routeA)).toBe(true);
  await choose([b]);
  await expectStatus(b, '文件', '本次查询完成');
  await expectStatus(b, 'Thread', '本次查询完成');
  await inactive();
  expect(new Set(queries('branchscope').map(item => item.route))).toEqual(new Set(owners.map(item => item.workspace_id)));
  for (const target of owners) for (const category of ['会话', 'Thread', '文件']) {
    await expectStatus(target, category, 'request_id:');
    await expectStatus(target, category, '不代表全局快照');
  }
  result.onlySelectedOwners = true;

  const pattern = '**/api/v1/**';
  const delayQuery = async (query, path) => {
    const gate = Promise.withResolvers(), pending = new Set(), blocked = [];
    const handler = async route => {
      const request = route.request();
      if (!isSearch(request) || queryOf(request) !== query || path && new URL(request.url()).pathname !== path) { await route.continue(); return; }
      const task = (async () => {
        const response = await route.fetch();
        blocked.push({ path: new URL(request.url()).pathname, route: request.headers()['x-boxteam-workspace-id'] });
        await gate.promise;
        await route.fulfill({ response });
      })();
      pending.add(task);
      try { await task; } finally { pending.delete(task); }
    };
    await page.route(pattern, handler);
    return { blocked, async release() { gate.resolve(); await Promise.all([...pending]); await page.unroute(pattern, handler); } };
  };
  result.phase = 'cancel-queued-and-late';
  const cancellation = await delayQuery('slowcancel');
  try {
    await input.fill('slowcancel');
    await expect.poll(() => cancellation.blocked.length).toBe(3);
    expect(queries('slowcancel')).toHaveLength(3);
    await input.fill('branchscope');
    await expectStatus(b, '文件', '本次查询完成');
    await inactive();
  } finally { await cancellation.release(); }
  await expect.poll(() => result.cancelled.filter(item => item.query === 'slowcancel').length).toBe(3);
  expect(queries('slowcancel')).toHaveLength(3);
  await expect(quick).not.toContainText('slowcancel');
  result.cancelledQueueAndLateResponses = true;

  result.phase = 'real-timeout-and-late';
  const delayed = await delayQuery('branchscope', '/api/v1/session-catalog/search');
  try {
    await input.fill('branchscope');
    // 相同输入不重复发送。
    const before = queries('branchscope').length;
    await page.waitForTimeout(250);
    expect(queries('branchscope').length).toBe(before);
    await input.fill('branchscope A');
    await expectStatus(a, '文件', '没有匹配的资源');
    await input.fill('branchscope');
    await expect.poll(() => delayed.blocked.length).toBe(2);
    await expectStatus(b, 'Thread', '本次查询完成');
    await expectStatus(a, '会话', '查询超时', { timeout: 18_000 });
    await expectStatus(b, '会话', '查询超时');
    await expectStatus(b, '文件', '本次查询完成');
    await inactive();
  } finally { await delayed.release(); }
  await expectStatus(a, '会话', '查询超时');
  result.timeoutKeepsOtherBranches = true;

  result.phase = 'all-failed-and-single-branch-retry';
  const fail = async route => {
    if (!isSearch(route.request()) || queryOf(route.request()) !== 'allfailure') { await route.continue(); return; }
    await route.fulfill({ status: 503, contentType: 'application/json', headers: { 'X-Request-ID': 'req-injected-failure' },
      body: JSON.stringify({ request_id: 'req-injected-failure', detail: '显式测试故障：owner 读取失败' }) });
  };
  await page.route(pattern, fail);
  try {
    await input.fill('allfailure');
    for (const target of owners) for (const category of ['会话', 'Thread', '文件']) {
      await expectStatus(target, category, '查询失败');
      await expectStatus(target, category, 'req-injected-failure');
    }
    await inactive();
    await expect(quick).not.toContainText('没有匹配的资源');
    result.allFailuresRemainFailures = true;
  } finally { await page.unroute(pattern, fail); }
  const beforeRetry = result.requests.length;
  await (await showStatus(a, 'Thread')).hover();
  await status(a, 'Thread').getByRole('button', { name: '重试此查询', exact: true }).click();
  await expectStatus(a, 'Thread', '本次查询完成');
  await expectStatus(b, 'Thread', '查询失败');
  await inactive();
  expect(result.requests.slice(beforeRetry)).toEqual([{ path: '/api/v1/session-threads/search', query: 'allfailure', route: routeA, deadline: undefined }]);
  result.retryOnlyFailedBranch = true;

  result.phase = 'offline-scope';
  await choose([c]);
  await expectStatus(c, '会话', '当前不可查询');
  await inactive();
  expect(result.requests.every(item => item.route !== c.workspace_id)).toBe(true);
  result.offlineHasNoBusinessQuery = true;

  result.phase = 'identity-recovery-revokes-picks';
  const expire = async route => {
    if (!isSearch(route.request()) || queryOf(route.request()) !== 'identityfail') { await route.continue(); return; }
    await route.fulfill({ status: 401, contentType: 'application/json', headers: { 'X-Request-ID': 'req-injected-identity' },
      body: JSON.stringify({ request_id: 'req-injected-identity', detail: { code: 'user_session_required' } }) });
  };
  await page.route(pattern, expire);
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
  } finally { await page.unroute(pattern, expire); }
  await page.keyboard.press('Escape');
  await Promise.all(responseReads);
  expect(responseErrors).toEqual([]);
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
