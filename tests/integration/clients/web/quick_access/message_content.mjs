import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!base || !output) throw new Error('消息正文验收缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_message_content.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], searches: [], around: [],
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
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/sessions/history/search') result.searches.push({ path, body: request.postDataJSON(),
      route: request.headers()['x-boxteam-workspace-id'], deadline: request.headers()['x-boxteam-content-search-deadline'] });
    if (/\/threads\/[^/]+\/history$/.test(path) && request.postDataJSON()?.direction === 'around') {
      result.around.push({ path, body: request.postDataJSON(), route: request.headers()['x-boxteam-workspace-id'] });
    }
  });
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
    await testControl({ action: 'plan_model', session_id: sessionId, thread_id: thread, interactions: [interactions.shift()] });
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
  expect(await page.locator(`section.boxteam-thread[data-thread-id="${child}"]`).count()).toBe(0);
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  await command('workbench.action.quickOpen', query);
  await expect(quick).toContainText('文件 · 没有匹配的资源');
  await page.keyboard.press('Escape');
  await command('workbench.action.quickOpen', 'content ' + query);
  await expect(quick).toContainText('会话消息');
  expect(result.searches).toHaveLength(0);
  result.unselectedHasNoContentQuery = true;
  await quick.locator('[data-quick-input-id="content-category-message"]').click();
  await expect(quick).toContainText('会话消息 · 本次查询完成');
  expect(result.searches).toHaveLength(1);
  expect(result.searches[0].route).toBe(target.workspace_id);
  expect(Number(result.searches[0].deadline)).toBeGreaterThan(Date.now() / 1000 - 15);
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
  result.firstPick = firstId;
  result.owner = { gateway: health.gateway_id, workspace: target.backend_workspace_id, sessionId, main, child };
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error.stack ?? error);
  if (page) {
    result.quickInputSnapshot = await page.locator('.quick-input-widget').ariaSnapshot().catch(() => '浮层已关闭');
    await page.screenshot({ path: output.replace(/\.json$/, '.failure.png') }).catch(error => { result.screenshotError = String(error); });
  }
} finally {
  await writeFile(output, JSON.stringify(result, null, 2));
  await run.close();
}
if (failure) throw failure;
