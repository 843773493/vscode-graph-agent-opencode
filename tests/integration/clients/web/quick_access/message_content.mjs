import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { expectLastSearchStatus } from './default_interactions.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!base || !output) throw new Error('消息正文验收缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_message_content.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], searches: [], nameSearches: [], around: [], queries: [],
  boundary: '真实 Gateway／正式 Workbench；消息和 fork 经产品 API 创建；model_stream 精确回放，不预写历史或缓存' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  const queries = new Map();
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (/\/api\/(gateway\/(health|workspaces)|v1\/(session-catalog\/search|session-threads\/search|workspace\/files\/roots))$/.test(path)) {
      const observation = { url: request.url(), phase: result.phase, state: 'pending' };
      queries.set(request, observation);
      result.queries.push(observation);
    }
    if (['/api/gateway/search', '/api/v1/sessions/history/search', '/api/v1/search'].includes(path)) {
      const body = request.postDataJSON();
      const observed = { path, body, route: request.headers()['x-boxteam-workspace-id'], deadline: request.headers()['x-boxteam-content-search-deadline'] };
      // 默认入口现在也使用联邦请求；按领域操作分别观察，继续捕获旧正文直连接口。
      const destination = path === '/api/gateway/search' && body.queries.every(query => query.operation === 'name') ? result.nameSearches : result.searches;
      destination.push(observed);
    }
    if (/\/threads\/[^/]+\/history$/.test(path) && request.postDataJSON()?.direction === 'around') {
      result.around.push({ path, body: request.postDataJSON(), route: request.headers()['x-boxteam-workspace-id'] });
    }
  });
  page.on('requestfinished', request => { const query = queries.get(request); if (query) query.state = 'finished'; });
  page.on('requestfailed', request => { const query = queries.get(request); if (query) { query.state = 'failed'; query.error = request.failure(); } });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const api = async (path, data) => page.evaluate(async ({ path, data }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' },
      body: data === undefined ? undefined : JSON.stringify(data) });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return body.data;
  }, { path, data });
  // 通过真实页面 fetch 设置 Cookie，避免 Bun 的 Node HTTP 兼容层影响 Playwright APIRequestContext。
  await api('/api/gateway/users/guest', { tracking: { source: 'message-content-search' } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces?check_health=false');
  const target = targets.items.find(item => item.workspace_id === targets.active_workspace_id);
  const session = await api('/api/v1/sessions', { title: '消息原文定位验收' });
  const sessionId = session.session_id, main = (await api(`/api/v1/sessions/${sessionId}/main-thread`)).thread_id;
  const messagePath = thread => `/api/v1/sessions/${sessionId}/threads/${thread}/messages`;
  const interactions = JSON.parse(process.env.BOXTEAM_MESSAGE_SEARCH_INTERACTIONS);
  const send = async (thread, content) => {
    await testControl({ action: 'plan_model', session_id: sessionId, thread_id: thread,
      interactions: interactions.splice(0, interactions.length === 8 ? 2 : 1) });
    const accepted = await api(messagePath(thread), { message: { content }, run: {} });
    await expect.poll(async () => {
      const job = await api(`/api/v1/jobs/${accepted.job_id}`);
      if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
      return job.status;
    }, { timeout: 30_000 }).toBe('completed');
    return accepted;
  };
  const query = '定位😀词', original = '起始😀\n' + 'a'.repeat(66_000) + query + '\n第二处' + query + '\n尾';
  const first = await send(main, original);
  for (let index = 0; index < 6; index += 1) await send(main, `后续轮次 ${index}`);
  const source = await api(`/api/v1/sessions/${sessionId}/fork-source-point`);
  const fork = await api(`/api/v1/sessions/${sessionId}/fork-context`, {
    idempotency_key: 'message-content-child', source_uri: source.source_uri, source_view_id: source.source_view_id,
    source_view_revision: source.source_view_revision, anchor_item_id: source.anchor_item_id,
    target_container_uri: source.child_container_uri, mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  const child = fork.thread_id;
  expect(child).not.toBe(main);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  // 刷新按钮先于工作区镜像出现；等产品导航实际读到刚创建的 Session 后再发起搜索。
  await expect(page.getByRole('treeitem', { name: /^消息原文定位验收/ })).toBeVisible();
  expect(await page.locator(`section.boxteam-thread[data-thread-id="${child}"]`).count()).toBe(0);
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await command('workbench.action.quickOpen', query);
  await expectLastSearchStatus({ page, quick, label: '文件 · 没有匹配的资源' });
  await page.keyboard.press('Escape');
  await command('workbench.action.quickOpen', 'content ' + query);
  await expect(quick).toContainText('会话消息');
  // 名称立即提交，文件等待根元数据；两次请求必须保持同一冻结 owner。
  expect(result.nameSearches).toHaveLength(2);
  const owner = { gateway_id: health.gateway_id, workspace_id: target.backend_workspace_id };
  expect(result.nameSearches[0].body.scope).toEqual({ mode: 'selected', owners: [owner] });
  expect(result.nameSearches[0].body.queries.map(({ category, operation, match_mode }) => ({ category, operation, match_mode }))).toEqual([
    { category: 'catalog', operation: 'name', match_mode: 'fuzzy' }, { category: 'thread', operation: 'name', match_mode: 'fuzzy' },
  ]);
  const fileSearch = result.nameSearches[1];
  expect(fileSearch.path).toBe('/api/gateway/search');
  if (fileSearch.body.scope.mode === 'resume') {
    expect(fileSearch.body.scope.scope).toMatchObject({ version: 1, gateway_id: health.gateway_id, mode: 'selected', owners: [owner] });
  } else {
    expect(fileSearch.body.scope).toEqual({ mode: 'selected', owners: [owner] });
  }
  expect(fileSearch.body.queries).toHaveLength(1);
  const fileQuery = fileSearch.body.queries[0];
  expect(fileQuery).toMatchObject({ category: 'file', operation: 'name', match_mode: 'fuzzy', query });
  expect(fileQuery.file_targets).toHaveLength(1);
  const rootUri = `boxteam://workspace/${owner.workspace_id}/resources/files/root`;
  expect(fileQuery.file_targets[0]).toMatchObject({ owner, target: { uri: rootUri } });
  if (fileSearch.body.scope.mode === 'resume') {
    expect(fileSearch.body.continuations).toEqual([{ branch: { owner, category: 'file', operation: 'name', resource_uri: rootUri } }]);
  }
  expect(result.searches).toHaveLength(0);
  result.unselectedHasNoContentQuery = true;
  await quick.locator('[data-quick-input-id="content-category-message"]').click();
  await expect(quick).toContainText('会话消息 · 本次查询完成');
  expect(result.searches).toHaveLength(1);
  expect(result.searches[0].path).toBe('/api/gateway/search');
  expect(result.searches[0].body.scope).toEqual({ mode: 'selected', owners: [{ gateway_id: health.gateway_id, workspace_id: target.backend_workspace_id }] });
  expect(result.searches[0].body.queries.map(query => query.category)).toEqual(['message']);
  expect(result.searches[0].body.deadline_at).toBeGreaterThan(Date.now() / 1000 - 15);
  const picks = quick.locator(`[data-quick-input-id^="boxteam-thread://"][data-quick-input-id*="/${child}#"]`);
  await expect(picks).toHaveCount(2);
  const firstId = await picks.first().getAttribute('data-quick-input-id');
  result.phase = 'open-unloaded-child';
  await picks.first().hover();
  await picks.first().getByRole('button', { name: '在侧边打开', exact: true }).click();
  const groups = page.locator('.editor-group-container');
  await expect(groups).toHaveCount(2);
  const thread = groups.nth(1).locator(`section.boxteam-thread[data-workspace-id="${target.backend_workspace_id}"][data-session-id="${sessionId}"][data-thread-id="${child}"]`);
  await expect(thread).toBeVisible();
  const mark = thread.locator('.chat-message-location mark');
  await expect(mark).toHaveText(query);
  await expect(thread).toContainText('已定位消息原文');
  expect(await thread.locator('.chat-message-location pre').textContent()).toBe(original);
  const visibleMark = () => mark.evaluate(element => {
    const bounds = element.getBoundingClientRect(), viewport = element.closest('[data-virtuoso-scroller]').getBoundingClientRect();
    return bounds.bottom > viewport.top && bounds.top < viewport.bottom;
  });
  await expect.poll(visibleMark).toBe(true);
  expect(result.around.at(-1).path).toContain(`/threads/${child}/history`);
  expect(result.around.at(-1).body.anchor_turn_id).toBe(first.turn_id);
  expect(result.around.at(-1).body.anchor_message.start_codepoint).toBeGreaterThan(65_536);
  result.unopenedChildLocated = result.longOriginalRangeVisible = true;

  // 复用同一个 Pane 接受另一处命中，不能沿用第一次位置或跳到 main。
  await command('workbench.action.quickOpen', 'content ' + query);
  await quick.locator('[data-quick-input-id="content-category-message"]').click();
  await expect(quick).toContainText('会话消息 · 本次查询完成');
  await quick.locator(`[data-quick-input-id^="boxteam-thread://"][data-quick-input-id*="/${child}#"]`).last().click();
  await expect.poll(() => result.around.length).toBe(2);
  await expect(thread).toContainText('已定位消息原文');
  expect(result.around[1].body.anchor_message.start_codepoint).toBeGreaterThan(result.around[0].body.anchor_message.start_codepoint);
  await expect.poll(visibleMark).toBe(true);
  result.reusedPaneRevealsAnotherMatch = true;
  await thread.getByRole('button', { name: '关闭定位原文', exact: true }).click();
  await expect(mark).toHaveCount(0);

  // 显式注入旧版本参数，响应仍由真实 owner 核验；不修改响应或持久历史。
  result.phase = 'stale-reference';
  await command('workbench.action.quickOpen', 'content ' + query);
  await quick.locator('[data-quick-input-id="content-category-message"]').click();
  await expect(quick).toContainText('会话消息 · 本次查询完成');
  const stalePattern = `**/threads/${child}/history`;
  const stale = async route => {
    const data = route.request().postDataJSON();
    await route.continue({ postData: JSON.stringify({ ...data, anchor_message: { ...data.anchor_message, content_hash: 'changed' } }) });
  };
  await page.route(stalePattern, stale);
  try {
    const response = page.waitForResponse(item => item.url().endsWith(`/threads/${child}/history`) && item.status() === 409);
    await quick.locator(`[data-quick-input-id^="boxteam-thread://"][data-quick-input-id*="/${child}#"]`).first().click();
    expect((await (await response).json()).detail.code).toBe('stale_turn_reference');
    await expect(thread).toContainText('消息命中已过期，请重新搜索');
    await expect(mark).toHaveCount(0);
    await expect(thread.locator('textarea')).toBeEnabled();
    result.staleReferenceRejected = true;
  } finally { await page.unroute(stalePattern, stale); }
  await command('workbench.action.quickOpen', 'content README');
  await quick.locator('[data-quick-input-id="content-category-file"]').click();
  await expect(input).toHaveValue('%README');
  await expect(input).toHaveAttribute('placeholder', /搜索文件内容/);
  result.fileUsesOriginalProvider = true;
  await page.keyboard.press('Escape');

  result.phase = 'tool-result-content';
  const toolQuery = process.env.BOXTEAM_TOOL_SEARCH_QUERY;
  const toolFork = await api(`/api/v1/sessions/${sessionId}/fork-context`, {
    idempotency_key: 'tool-content-child', source_uri: source.source_uri, source_view_id: source.source_view_id,
    source_view_revision: source.source_view_revision, anchor_item_id: source.anchor_item_id,
    target_container_uri: source.child_container_uri, mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  expect(await page.locator(`section.boxteam-thread[data-thread-id="${toolFork.thread_id}"]`).count()).toBe(0);
  await command('workbench.action.quickOpen', 'content ' + toolQuery);
  await quick.locator('[data-quick-input-id="content-category-message"]').click();
  await expect(quick).toContainText('会话消息 · 没有匹配的资源');
  expect(await quick.locator('[data-quick-input-id^="boxteam-thread://"]').count()).toBe(0);
  await page.keyboard.press('Escape');
  await command('workbench.action.quickOpen', 'content ' + toolQuery);
  await quick.locator('[data-quick-input-id="content-category-tool"]').click();
  await expect(quick).toContainText('工具结果 · 本次查询完成');
  const toolPick = quick.locator(`[data-quick-input-id^="boxteam-thread://"][data-quick-input-id*="/${toolFork.thread_id}#"]`);
  await expect(toolPick).toHaveCount(1);
  // 原版详情悬停会覆盖下方行；从行左边缘进入，再点击右侧的真实动作。
  await input.hover();
  await toolPick.hover({ position: { x: 5, y: 5 } });
  await toolPick.getByRole('button', { name: '在侧边打开', exact: true }).click();
  const toolThread = page.locator(`section.boxteam-thread[data-workspace-id="${target.backend_workspace_id}"][data-session-id="${sessionId}"][data-thread-id="${toolFork.thread_id}"]`);
  await expect(toolThread).toBeVisible();
  const toolOriginal = toolThread.locator('.chat-tool-details .chat-message-location');
  await expect(toolOriginal.locator('mark')).toHaveText(toolQuery);
  expect(createHash('sha256').update(await toolOriginal.locator('pre').textContent()).digest('hex')).toBe(process.env.BOXTEAM_TOOL_SEARCH_SHA256);
  await expect(toolThread).toContainText('已定位消息原文');
  await expect.poll(() => toolOriginal.locator('mark').evaluate(element => {
    const bounds = element.getBoundingClientRect(), viewport = element.closest('[data-virtuoso-scroller]').getBoundingClientRect();
    const details = element.closest('.chat-thinking-body').getBoundingClientRect();
    return bounds.bottom > Math.max(viewport.top, details.top) && bounds.top < Math.min(viewport.bottom, details.bottom);
  })).toBe(true);
  expect(result.around.at(-1).body.anchor_message.category).toBe('tool_result');
  expect(result.around.at(-1).body.anchor_message.start_codepoint).toBeGreaterThan(65536);
  expect(result.searches.at(-1).body.queries.map(query => query.category)).toEqual(['tool_result']);
  expect(result.searches.every(request => request.path === '/api/gateway/search')).toBe(true);
  result.unopenedToolChildLocated = result.fullToolOutputVisible = result.toolCategoryIsolated = true;
  result.firstPick = firstId;
  result.owner = { gateway: health.gateway_id, workspace: target.backend_workspace_id, sessionId, main, child };
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error.stack ?? error);
  if (page) {
    result.chatViewport = await page.locator('.chat-stream-shell').evaluateAll(shells => shells.map(shell => {
      const stream = shell.querySelector('[data-virtuoso-scroller]'), mark = shell.querySelector('.chat-message-location mark');
      return { notice: shell.querySelector('.chat-history-notice')?.textContent,
        scroll: stream ? { top: stream.scrollTop, height: stream.scrollHeight, client: stream.clientHeight } : null,
        mark: mark ? { top: mark.getBoundingClientRect().top, bottom: mark.getBoundingClientRect().bottom } : null };
    }));
    result.quickInputSnapshot = await page.locator('.quick-input-widget').ariaSnapshot().catch(() => '浮层已关闭');
    await page.screenshot({ path: output.replace(/\.json$/, '.failure.png') }).catch(error => { result.screenshotError = String(error); });
  }
} finally {
  await writeFile(output, JSON.stringify(result, null, 2));
  await run.close();
}
if (failure) throw failure;
