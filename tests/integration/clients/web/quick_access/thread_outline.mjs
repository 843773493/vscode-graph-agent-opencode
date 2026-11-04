import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!base || !output || !screenshot) throw new Error('Thread 大纲缺少隔离目标或产物目录');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_thread_outline.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], history: [], searches: [], viewWrites: [], executions: [],
  boundary: '真实 Gateway、正式 Workbench、canonical 产品执行及 fork；模型使用现有 model_stream，未手写历史或浏览器状态' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1500, height: 1000 }, reducedMotion: 'reduce' });
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/history')) result.history.push({ path, body: request.postDataJSON() });
    if (path.endsWith('/search')) result.searches.push(path);
    if (request.method() !== 'GET' && path.includes('/view-state')) result.viewWrites.push({ path, body: request.postDataJSON() });
  });
  const api = (path, body) => page.evaluate(async ({ path, body }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': token, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, body });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await api('/api/gateway/users/guest', { tracking: { source: 'thread-outline' } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces');
  const target = targets.items.find(item => item.workspace_id === targets.active_workspace_id);
  if (!target) throw new Error('大纲验收缺少已激活工作区');
  const workspace = await api('/api/v1/workspace');
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  result.phase = 'empty-thread';
  const empty = await api('/api/v1/sessions', { title: '没有记录的大纲' });
  const emptyThread = (await api(`/api/v1/sessions/${empty.session_id}/main-thread`)).thread_id;
  // 不传编辑器选项，让原命令沿 IOpenerService 解析规范资源 URI。
  await command('_workbench.open', `boxteam://workspace/${workspace.workspace_id}/resources/session/${empty.session_id}/threads/${emptyThread}`);
  await expect(page.locator(`section.boxteam-thread[data-thread-id="${emptyThread}"] textarea`)).toBeEnabled({ timeout: 30_000 });
  await command('workbench.action.quickOpen', '@');
  await expect(quick).toContainText('No matching entries');
  await page.keyboard.press('Escape');
  result.emptyThreadOutline = true;
  const session = await api('/api/v1/sessions', { title: '大纲分页验收' });
  const main = (await api(`/api/v1/sessions/${session.session_id}/main-thread`)).thread_id;
  const interactions = JSON.parse(process.env.BOXTEAM_OUTLINE_INTERACTIONS);
  const send = async (thread, content) => {
    const started = performance.now();
    await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: thread, interactions: [interactions.shift()] });
    const accepted = await api(`/api/v1/sessions/${session.session_id}/threads/${thread}/messages`, { message: { content }, run: {} });
    await expect.poll(async () => {
      const job = await api(`/api/v1/jobs/${accepted.job_id}`);
      if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
      return job.status;
    // TODO：独立量化长历史执行准备的增长成本；这不是大纲交互的等待期限。
    }, { timeout: 60_000 }).toBe('completed');
    result.executions.push({ thread, turn: accepted.turn_id, job: accepted.job_id, elapsedMs: performance.now() - started });
    return accepted;
  };
  await send(main, '共同起点😀 $(warning)');
  const source = await api(`/api/v1/sessions/${session.session_id}/fork-source-point`);
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, {
    idempotency_key: 'thread-outline-child', source_uri: source.source_uri, source_view_id: source.source_view_id,
    source_view_revision: source.source_view_revision, anchor_item_id: source.anchor_item_id,
    target_container_uri: source.child_container_uri, mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  const child = fork.thread_id;
  result.phase = 'generate-product-history';
  for (let index = 1; index <= 22; index++) await send(child, `child 大纲消息 ${String(index).padStart(2, '0')}`);
  await send(main, '仅 main 可见的后续消息');
  const uri = `boxteam://workspace/${workspace.workspace_id}/resources/session/${session.session_id}/threads/${child}`;
  await command('_workbench.open', uri);
  const groups = page.locator('.editor-group-container');
  const firstPane = groups.first().locator(`section.boxteam-thread[data-thread-id="${child}"]`);
  await expect(firstPane.locator('textarea')).toBeEnabled({ timeout: 30_000 });
  await expect(firstPane).toContainText('child 大纲消息 22');
  await command('workbench.action.splitEditorRight');
  await expect(groups).toHaveCount(2);
  const secondPane = groups.nth(1).locator(`section.boxteam-thread[data-thread-id="${child}"]`);
  await expect(secondPane.locator('textarea')).toBeEnabled({ timeout: 30_000 });
  await expect(secondPane).toContainText('child 大纲消息 22');
  const viewport = pane => pane.locator('[data-virtuoso-scroller]');
  const position = pane => viewport(pane).evaluate(element => {
    const anchor = [...element.querySelectorAll('[data-turn-id]')].find(item => item.getBoundingClientRect().bottom > element.getBoundingClientRect().top);
    return { top: element.scrollTop, height: element.scrollHeight, anchor: anchor?.dataset.turnId,
      offset: anchor ? element.getBoundingClientRect().top - anchor.getBoundingClientRect().top : 0 };
  });
  // 只读原持久视图；位置必须由真实滚轮产生，预览不得覆写它。
  const storedPosition = () => page.evaluate(async key => {
    if (!(await indexedDB.databases()).some(database => database.name === 'boxteam-browser-views')) return null;
    return new Promise((resolve, reject) => {
      const opening = indexedDB.open('boxteam-browser-views');
      opening.onerror = () => reject(opening.error);
      opening.onsuccess = () => {
        const database = opening.result, transaction = database.transaction('views', 'readonly');
        const request = transaction.objectStore('views').get(key);
        transaction.oncomplete = () => { database.close(); resolve(request.result ?? null); };
        transaction.onabort = () => { database.close(); reject(transaction.error); };
      };
    });
  }, JSON.stringify([health.gateway_id, 'session', target.workspace_id, session.session_id, child]));
  await expect.poll(async () => (await position(secondPane)).top).toBeGreaterThan(100);
  const originalFirst = await position(firstPane), secondBottom = await position(secondPane);
  await viewport(secondPane).hover(); await page.mouse.wheel(0, -420);
  await expect.poll(async () => (await position(secondPane)).top).toBeLessThan(secondBottom.top - 100);
  const originalSecond = await position(secondPane);
  await expect.poll(async () => (await storedPosition())?.turn_anchor).toBe(originalSecond.anchor);
  const storedBefore = await storedPosition();
  expect(storedBefore.follow_latest).toBe(false);
  result.phase = 'outline-page-preview';
  const before = result.history.length, writes = result.viewWrites.length;
  await command('workbench.action.quickOpen', '@');
  await expect(quick).toContainText('已载入 20 轮，更早历史未载入');
  await expect(quick).not.toContainText('共同起点');
  await expect(quick).not.toContainText('仅 main 可见');
  const outlineReads = () => result.history.slice(before).filter(read => read.body.include?.join(',') === 'user,metadata');
  expect(outlineReads()).toHaveLength(1);
  await input.fill('@共同起点');
  await expect(quick).toContainText('加载更早轮次');
  const historyPattern = `**/threads/${child}/history`;
  const invalidCursor = async route => {
    const body = route.request().postDataJSON();
    await route.continue({ postData: JSON.stringify(body.direction === 'before' ? { ...body, cursor: 'invalid-outline-cursor' } : body) });
  };
  await page.route(historyPattern, invalidCursor);
  try {
    await quick.getByRole('option', { name: /加载更早轮次/ }).click();
    await expect(quick).toContainText('历史读取失败');
    await expect(quick).not.toContainText('本次历史已覆盖');
  } finally { await page.unroute(historyPattern, invalidCursor); }
  await quick.getByRole('option', { name: /加载更早轮次/ }).click();
  // 重读时覆盖文案仍是旧页；等待原加载状态结束后，才能明确请求下一页。
  await expect(quick.getByRole('option', { name: /加载更早轮次/ })).toContainText('加载更早轮次');
  await expect(quick).toContainText('已载入 20 轮，更早历史未载入');
  await quick.getByRole('option', { name: /加载更早轮次/ }).click();
  await expect(quick).toBeVisible();
  await expect(quick).toContainText('共同起点');
  await expect(quick).toContainText('已载入 23 轮，本次历史已覆盖');
  expect(outlineReads()).toHaveLength(4);
  expect(outlineReads().map(read => read.body.direction)).toEqual(['tail', 'before', 'tail', 'before']);
  expect(outlineReads()[3].body.cursor).toBeTruthy();
  result.coverageAndExplicitPage = true;
  await page.keyboard.press('ArrowDown');
  await expect(secondPane).toContainText(/已定位第 \d+ 轮/);
  const preview = await position(secondPane);
  expect(preview.anchor).not.toBe(originalSecond.anchor);
  expect((await position(firstPane)).anchor).toBe(originalFirst.anchor);
  expect(result.viewWrites.length).toBe(writes);
  expect(await storedPosition()).toEqual(storedBefore);
  await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible();
  await expect.poll(async () => (await position(secondPane)).anchor).toBe(originalSecond.anchor);
  await expect.poll(async () => Math.abs((await position(secondPane)).offset - originalSecond.offset)).toBeLessThan(2);
  expect((await position(firstPane)).anchor).toBe(originalFirst.anchor);
  expect(await storedPosition()).toEqual(storedBefore);
  result.previewNotPersisted = true;
  result.previewRestored = result.groupsIsolated = true;

  result.phase = 'accept-unmounted-turn';
  await command('workbench.action.quickOpen', '@child 大纲消息 05');
  const match = quick.getByRole('option', { name: /child 大纲消息 05/ });
  await expect(match).toHaveCount(1);
  await page.keyboard.press('Enter');
  await expect(quick).not.toBeVisible();
  await expect(secondPane).toContainText(/已定位第 \d+ 轮/);
  await expect.poll(async () => viewport(secondPane).evaluate(element => [...element.querySelectorAll('[data-turn-id]')].some(item => {
    const bounds = item.getBoundingClientRect(), view = element.getBoundingClientRect();
    return item.textContent.includes('child 大纲消息 05') && bounds.bottom > view.top && bounds.top < view.bottom;
  }))).toBe(true);
  expect((await position(firstPane)).anchor).toBe(originalFirst.anchor);
  result.unloadedTurnAccepted = true;
  result.noBusinessSearch = result.searches.length === 0;
  result.preciseChildHistory = result.history.slice(before).every(read => read.path.includes(`/threads/${child}/history`));

  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error.stack ?? error);
  if (page) {
    result.quickInput = await page.locator('.quick-input-widget').ariaSnapshot().catch(() => '浮层已关闭');
    result.threadSnapshots = await page.locator('section.boxteam-thread').evaluateAll(panes => panes.map(pane => ({
      thread: pane.dataset.threadId, text: pane.textContent.slice(-2500),
    })));
    await page.screenshot({ path: screenshot }).catch(error => { result.screenshotError = String(error); });
  }
} finally {
  try { await writeFile(output, JSON.stringify(result, null, 2)); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '大纲验收与结果保存均失败') : error; }
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '大纲验收与清理均失败') : error; }
}
if (failure) throw failure;
