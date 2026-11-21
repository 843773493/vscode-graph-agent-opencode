import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { planChatCalls, testControl } from '../../../../support/e2e_control/browser_client.mjs';

const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACT_ROOT;
if (!artifacts) throw new Error('必须设置浏览器产物目录');
const result = { phase: 'bootstrap', pageErrors: [], receipts: [], reads: [], opened: [], copied: [],
  timings: [],
  boundary: '真实 Workbench/Gateway/Backend；模型使用既有 model_stream 精确回放和上游响应屏障' };
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, permissions: ['clipboard-read', 'clipboard-write'] });
await context.tracing.start({ screenshots: true, snapshots: false, sources: false });
const page = await context.newPage();
page.on('pageerror', error => result.pageErrors.push(String(error)));
page.on('request', request => {
  if (new URL(request.url()).pathname.endsWith('/items/inspect')) result.reads.push(request.postDataJSON());
});
const gates = new Set();
try {
  await page.goto(process.env.BOXTEAM_BROWSER_BASE_URL, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  await command('workbench.action.closeAllEditors');
  const api = async (path, init = {}) => {
    const receipt = await page.evaluate(async ({ path, init }) => {
      const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...init.headers } });
      const body = await response.json(), header = response.headers.get('X-Request-ID');
      if (!response.ok || body.code !== 0 || !body.request_id || body.request_id !== header) throw new Error(JSON.stringify({ path, status: response.status, body }));
      return { data: body.data, path, requestId: body.request_id, header };
    }, { path, init });
    result.receipts.push({ path, requestId: receipt.requestId, header: receipt.header });
    return receipt.data;
  };
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const owner = workspaces.active_workspace;
  expect(owner.gateway_id).toBe(health.gateway_id);
  const headers = { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id };
  expect((await api('/api/v1/workspace', { headers })).workspace_id).toBe(owner.workspace_id);
  const created = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/sessions');
  await page.locator('.native-session-navigation .new-session-pill').click();
  const createResponse = await created;
  expect(createResponse.ok()).toBe(true);
  const session = (await createResponse.json()).data;
  result.owner = { ...owner, session_id: session.session_id, thread_id: session.thread_id };
  const base = `/api/v1/sessions/${session.session_id}`, threadBase = `${base}/threads/${session.thread_id}`;
  const uri = `boxteam://${owner.gateway_id}/workspace/${owner.workspace_id}/resources/session/${session.session_id}/threads/${session.thread_id}`;
  const surface = () => page.locator(`section.boxteam-thread[data-session-id="${session.session_id}"][data-thread-id="${session.thread_id}"]`);
  await expect(surface()).toBeVisible({ timeout: 30_000 });
  const send = async text => {
    const composer = surface().locator('[data-testid="composer-input"]');
    await expect(composer).toBeEnabled({ timeout: 30_000 });
    await composer.fill(text);
    const receipt = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `${threadBase}/messages`);
    await surface().locator('[data-testid="composer-send"]').click();
    const response = await receipt;
    expect(response.ok()).toBe(true);
    return (await response.json()).data;
  };
  const history = () => api(`${threadBase}/history`, { method: 'POST', headers, body: JSON.stringify({ direction: 'tail' }) });
  const acceptMenu = async name => {
    const target = page.getByRole('menuitem', { name, exact: true });
    await expect(target).toBeVisible();
    await target.focus();
    // 用原 ActionBar 的导航更新其 focusedItem；只聚焦 DOM 不会更新已聚焦菜单的内部索引。
    await page.keyboard.press('Home');
    const count = await page.getByRole('menuitem').count();
    for (let index = 0; index < count && !await target.evaluate(element => element.matches(':focus')); index++) {
      await page.keyboard.press('ArrowDown');
    }
    await expect(target).toBeFocused();
    await page.keyboard.press('Enter');
  };
  const markers = data => data.items.flatMap(turn => turn.response_parts.filter(part => part.kind === 'context_notice')
    .map(part => ({ ...part, turn_id: turn.turn_id })));
  const markerElement = part => surface().locator(`[data-context-marker~="${part.part_id}"]`);
  const beforeUser = async part => {
    const marker = markerElement(part);
    await expect(marker).toBeVisible({ timeout: 3000 });
    expect(await marker.evaluate(element => {
      const turn = element.closest('.chat-turn');
      const user = turn.querySelector('.chat-user-row');
      return !!user && !!(element.compareDocumentPosition(user) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
  };
  const markerLatency = async (part, budget) => {
    await beforeUser(part);
    // canonical 来源时间可能属于旧执行；派发起点由 Python 在原 model_calls 冷读核对。
    result.timings.push({ metric: 'call-created-to-marker-visible-upper-bound',
      modelCallId: part.context_model_call_id, visibleAtUnixMs: Date.now(), budgetMs: budget });
  };
  const verifyOrder = async (initial, reminder, firstTurnId, secondTurnId) => {
    await beforeUser(initial);
    expect(await markerElement(initial).evaluate(element => {
      const transcript = element.closest('.chat-transcript');
      return transcript.querySelector('.chat-context-notice, .chat-user-row, .chat-assistant-row') === element;
    })).toBe(true);
    if (!reminder) return;
    await beforeUser(reminder);
    expect(await markerElement(reminder).evaluate((element, ids) => {
      const turn = element.closest('.chat-turn'), transcript = element.closest('.chat-transcript');
      const previous = transcript.querySelector(`[data-turn-id="${ids.first}"] .chat-assistant-row`);
      const user = turn.querySelector('.chat-user-row'), answer = turn.querySelector('.chat-assistant-row');
      const before = (left, right) => !!left && !!right && !!(left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING);
      return turn.dataset.turnId === ids.second && before(previous, element) && before(element, user) && before(user, answer);
    }, { first: firstTurnId, second: secondTurnId })).toBe(true);
  };
  const viewSource = async (part, all) => {
    const marker = markerElement(part), before = result.reads.length;
    await marker.locator('.chat-context-marker').hover();
    await marker.locator('.chat-context-marker').click({ button: 'right' });
    await expect(page.getByRole('menuitem', { name: '查看上下文内容', exact: true })).toBeVisible();
    expect(result.reads).toHaveLength(before);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('menuitem', { name: '查看上下文内容', exact: true })).toBeHidden();
    expect(result.reads).toHaveLength(before);
    await marker.locator('.chat-context-marker').focus();
    await page.keyboard.press('Shift+F10');
    const contentStarted = performance.now();
    // 原版菜单在显示 100ms 后才接受 mouseup；键盘接受沿真实 ActionBar，不靠固定 sleep。
    await acceptMenu('查看上下文内容');
    const group = all.filter(value => value.context_model_call_id === part.context_model_call_id
      && value.context_anchor === part.context_anchor && value.context_before_item_id === part.context_before_item_id);
    if (group.length > 1) {
      const quick = page.locator('.quick-input-widget');
      await expect(quick).toBeVisible();
      expect(result.reads).toHaveLength(before);
      await quick.locator('.monaco-list-row').nth(group.findIndex(value => value.part_id === part.part_id)).click();
    }
    const editor = page.locator('.editor-group-container.active .editor-instance .monaco-editor').filter({ visible: true });
    await expect(editor).toBeVisible({ timeout: 3000 });
    await expect(editor.locator('.view-lines')).not.toBeEmpty();
    result.timings.push({ metric: 'accepted-source-to-editor-visible', elapsedMs: performance.now() - contentStarted });
    expect(performance.now() - contentStarted).toBeLessThanOrEqual(3000);
    await expect(editor.getByRole('textbox')).toHaveAttribute('aria-autocomplete', 'none');
    await editor.getByRole('textbox').focus();
    await page.keyboard.press('ControlOrMeta+A'); await page.keyboard.press('ControlOrMeta+C');
    const text = await page.evaluate(() => navigator.clipboard.readText());
    expect(text.length).toBeGreaterThan(0);
    expect(result.reads).toHaveLength(before + 1);
    expect(result.reads.at(-1)).toEqual({ operation: 'content', content_ref: part.context_content_ref });
    result.opened.push({ reference: part.context_content_ref, text });
    await command('workbench.action.closeActiveEditor');
    await expect(surface()).toBeVisible();
  };
  const planned = await planChatCalls(session.session_id, session.thread_id, [
    { label: 'marker-tool', calls: [{ id: 'marker-read', name: 'read_file', arguments: { path: 'README.md' } }] },
    { label: 'marker-first', text: '首轮等待中断。' }, { label: 'marker-second', text: '续发已完成。' },
  ], 'browser-chat-tool-loop-stub', process.env.BOXTEAM_BROWSER_MODEL_ENDPOINT);
  const arm = async id => { gates.add(id); await testControl({ action: 'arm', operation_id: id, phase: 'model.response_ready' }); };
  const release = async id => { await testControl({ action: 'release', operation_id: id, phase: 'model.response_ready' }); gates.delete(id); };
  await arm(planned[0]); await arm(planned[1]);
  result.phase = 'first-dispatch';
  const first = await send('U1：检查初始上下文后停止');
  await testControl({ action: 'acknowledged', operation_id: planned[0], phase: 'model.response_ready' });
  const firstPage = await history(), initial = markers(firstPage);
  result.initial = initial;
  expect(initial.map(part => part.context_source_id)).toEqual(expect.arrayContaining([
    'instruction:agent-instructions', 'mcp-tool-guidance:v1', 'agents:workspace',
  ]));
  expect(initial.every(part => part.turn_id === first.turn_id && part.context_anchor === 'before_user_message')).toBe(true);
  await markerLatency(initial[0], 3000);
  await verifyOrder(initial[0]);
  expect(result.reads).toHaveLength(0);
  await page.screenshot({ path: join(artifacts, '01-first-running.png') });
  await viewSource(initial.find(part => part.context_source_id === 'instruction:agent-instructions'), initial);
  // 复制与查看复用原动作 owner；选择来源、取消均不读正文，接受后只读所选一份。
  const copiedSource = initial.find(part => part.context_source_id === 'instruction:agent-instructions');
  const beforeCopy = result.reads.length;
  await markerElement(copiedSource).locator('.chat-context-marker').click({ button: 'right' });
  await acceptMenu('复制此项');
  await expect(page.locator('.quick-input-widget')).toBeVisible();
  expect(result.reads).toHaveLength(beforeCopy);
  await page.keyboard.press('Escape');
  expect(result.reads).toHaveLength(beforeCopy);
  await markerElement(copiedSource).locator('.chat-context-marker').click({ button: 'right' });
  await acceptMenu('复制此项');
  await expect(page.locator('.quick-input-widget')).toBeVisible();
  expect(result.reads).toHaveLength(beforeCopy);
  await page.evaluate(() => navigator.clipboard.writeText('等待上下文复制'));
  await page.locator('.quick-input-widget .monaco-list-row').nth(initial.indexOf(copiedSource)).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).not.toBe('等待上下文复制');
  expect(result.reads).toHaveLength(beforeCopy + 1);
  expect(result.reads.at(-1)).toEqual({ operation: 'content', content_ref: copiedSource.context_content_ref });
  result.copied.push({ reference: copiedSource.context_content_ref, text: await page.evaluate(() => navigator.clipboard.readText()) });
  await release(planned[0]);
  await testControl({ action: 'acknowledged', operation_id: planned[1], phase: 'model.response_ready' });
  result.toolResultIds = (await history()).items.flatMap(turn => turn.response_parts.filter(part => part.kind === 'tool_result').map(part => part.source.item_id));
  expect(result.toolResultIds.length).toBeGreaterThan(0);
  result.phase = 'interrupt';
  const interrupted = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `${base}/interrupt`);
  await surface().locator('#interruptButton').click();
  expect((await interrupted).ok()).toBe(true);
  await expect.poll(async () => (await api(`/api/v1/jobs/${first.job_id}`, { headers })).status,
    { timeout: 30_000 }).toBe('cancelled');
  await release(planned[1]);
  await arm(planned[2]);
  result.phase = 'second-dispatch';
  const second = await send('U2：继续并查看这次恢复提示');
  await testControl({ action: 'acknowledged', operation_id: planned[2], phase: 'model.response_ready' });
  const live = markers(await history());
  const reminders = live.filter(part => part.context_kind === 'checkpoint_reminder');
  expect(reminders.length).toBeGreaterThan(0);
  expect(reminders.every(part => part.turn_id === second.turn_id && part.context_anchor === 'before_user_message')).toBe(true);
  expect(live.filter(part => part.turn_id === first.turn_id).map(part => part.part_id)).toEqual(initial.map(part => part.part_id));
  await markerLatency(reminders[0], 2000);
  await verifyOrder(initial[0], reminders[0], first.turn_id, second.turn_id);
  await page.screenshot({ path: join(artifacts, '02-second-running.png') });
  await viewSource(reminders[0], live);
  await release(planned[2]);
  await expect.poll(async () => (await api(`/api/v1/jobs/${second.job_id}`, { headers })).status,
    { timeout: 30_000 }).toBe('completed');
  const frozen = markers(await history());
  const finalPage = await history();
  expect(finalPage.items.flatMap(turn => turn.response_parts.filter(part => part.kind === 'tool_result').map(part => part.source.item_id)))
    .toEqual(result.toolResultIds);
  const verifyHistory = async label => {
    const reads = result.reads.length;
    await expect(surface()).toBeVisible({ timeout: 30_000 });
    await verifyOrder(initial[0], reminders[0], first.turn_id, second.turn_id);
    expect(markers(await history())).toEqual(frozen);
    expect(result.reads).toHaveLength(reads);
    await page.screenshot({ path: join(artifacts, `${label}.png`) });
  };
  result.phase = 'refresh';
  await page.reload({ waitUntil: 'domcontentloaded' }); await verifyHistory('03-refreshed');
  result.phase = 'reopen';
  await command('workbench.action.closeActiveEditor');
  await command('_workbench.open', uri); await verifyHistory('04-reopened');
  await viewSource(reminders[0], frozen);
  // 普通 Item 仍能选择原 Markdown 打开方式；取消格式选择不传正文，接受后仅下载一次。
  const beforeItem = result.reads.length;
  const user = surface().locator(`[data-turn-id="${first.turn_id}"] .chat-user-bubble`);
  const openItemMenu = async () => {
    await user.click({ button: 'right' });
    await acceptMenu('打开内容…');
    await expect(page.locator('.quick-input-widget')).toBeVisible();
  };
  await openItemMenu();
  expect(result.reads.slice(beforeItem).map(read => read.operation)).toEqual(['content_metadata']);
  await page.keyboard.press('Escape');
  await openItemMenu();
  await page.locator('.quick-input-widget').getByText('Markdown 源文', { exact: true }).click();
  const ordinaryEditor = page.locator('.editor-group-container.active .editor-instance .monaco-editor').filter({ visible: true });
  await expect(ordinaryEditor.locator('.view-lines')).toContainText('U1：检查初始上下文后停止');
  expect(result.reads.slice(beforeItem).filter(read => read.operation === 'content')).toHaveLength(1);
  expect(result.reads.slice(beforeItem).map(read => read.operation)).toEqual(['content_metadata', 'content_metadata', 'content']);
  expect(result.reads.slice(beforeItem).filter(read => read.content_ref)).toHaveLength(0);
  result.ordinaryItemReads = result.reads.slice(beforeItem);
  await command('workbench.action.closeActiveEditor');
  // 只延迟尚未打开过的来源响应；正文仍由真实 Backend 生成，不替换业务结果。
  const cancelledSource = initial.find(part => part.context_source_id === 'agents:workspace');
  let releaseRead, requestArrived;
  const held = new Promise(resolve => { releaseRead = resolve; });
  const arrived = new Promise(resolve => { requestArrived = resolve; });
  const inspectionPattern = `**${threadBase}/items/inspect`;
  const holdRead = async route => {
    const payload = route.request().postDataJSON();
    if (payload.operation !== 'content' || JSON.stringify(payload.content_ref) !== JSON.stringify(cancelledSource.context_content_ref)) {
      await route.continue(); return;
    }
    const response = await route.fetch();
    requestArrived(route.request());
    await held;
    await route.fulfill({ response });
  };
  await page.route(inspectionPattern, holdRead);
  try {
    await markerElement(cancelledSource).locator('.chat-context-marker').click({ button: 'right' });
    await acceptMenu('查看上下文内容');
    const group = initial.filter(part => part.context_model_call_id === cancelledSource.context_model_call_id
      && part.context_anchor === cancelledSource.context_anchor && part.context_before_item_id === cancelledSource.context_before_item_id);
    await page.locator('.quick-input-widget .monaco-list-row').nth(group.findIndex(part => part.part_id === cancelledSource.part_id)).click();
    const request = await arrived;
    const failed = page.waitForEvent('requestfailed', { predicate: value => value === request, timeout: 3000 });
    await command('workbench.action.closeActiveEditor');
    releaseRead();
    await failed;
    result.closedRead = { reference: cancelledSource.context_content_ref, failure: request.failure() };
    await expect(surface()).toBeVisible();
  } finally {
    releaseRead();
    await page.unroute(inspectionPattern, holdRead);
  }
  const readsBeforeTheme = result.reads.length;
  const dark = () => page.locator('.monaco-workbench').evaluate(element => element.classList.contains('vs-dark'));
  const originalDark = await dark();
  await page.setViewportSize({ width: 720, height: 900 });
  await command('workbench.action.toggleLightDarkThemes');
  await expect.poll(dark).toBe(!originalDark);
  const narrowMarker = markerElement(cancelledSource).locator('.chat-context-marker');
  await narrowMarker.focus();
  await page.keyboard.press('Shift+F10');
  await expect(page.getByRole('menuitem', { name: '查看上下文内容', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  expect(result.reads).toHaveLength(readsBeforeTheme);
  await page.screenshot({ path: join(artifacts, '05-narrow-themed.png') });
  result.narrowThemeMenu = { width: 720, dark: await dark(), extraContentReads: 0 };
  await command('workbench.action.toggleLightDarkThemes');
  await expect.poll(dark).toBe(originalDark);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const samples = [];
  for (let index = 0; index < 20; index++) {
    const started = performance.now();
    const data = await api(`${threadBase}/items/inspect`, { method: 'POST', headers,
      body: JSON.stringify({ operation: 'content', content_ref: copiedSource.context_content_ref }) });
    const elapsedMs = performance.now() - started;
    expect(data.contents[0].content).toBe(result.copied[0].text);
    samples.push({ elapsedMs, bytes: Buffer.byteLength(JSON.stringify(data)), requestId: result.receipts.at(-1).requestId });
  }
  const sorted = samples.map(sample => sample.elapsedMs).sort((a, b) => a - b);
  const percentile = fraction => {
    const position = fraction * (sorted.length - 1), lower = Math.floor(position);
    return sorted[lower] + (sorted[Math.ceil(position)] - sorted[lower]) * (position - lower);
  };
  result.apiPerformance = { samples, p50Ms: percentile(0.5), p95Ms: percentile(0.95), maxMs: sorted.at(-1) };
  expect(result.apiPerformance.p95Ms).toBeLessThanOrEqual(500);
  expect(result.apiPerformance.maxMs).toBeLessThanOrEqual(1500);
  result.markers = frozen;
  result.phase = 'complete';
} catch (error) {
  result.error = String(error?.stack ?? error);
  await page.screenshot({ path: join(artifacts, 'failure.png') });
  throw error;
} finally {
  for (const id of gates) await testControl({ action: 'release', operation_id: id, phase: 'model.response_ready' });
  await writeFile(process.env.BOXTEAM_BROWSER_RESULT_PATH, JSON.stringify(result, null, 2));
  await context.tracing.stop({ path: join(artifacts, 'browser-trace.zip') });
  await browser.close();
}
