import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import { testControl, planChatCalls } from '../../../support/e2e_control/browser_client.mjs';
import { Perf } from '../../../harness/js/performance.mjs';
import { observePage } from '../../../harness/js/browser-performance.mjs';

const artifacts = process.env.BOXTEAM_MEMORY_ARTIFACTS;
const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
if (!artifacts || !baseUrl) throw new Error('必须显式提供本次产物目录和 Gateway 入口');
const run = process.env.BOXTEAM_MEMORY_RUN_LABEL ?? 'integration';
const baseline = process.env.BOXTEAM_MEMORY_BASELINE === '1';
await mkdir(artifacts, { recursive: true });
const cleanups = [];
const perf = new Perf({ performanceDir: artifacts, projectRoot: process.cwd(), runRoot: artifacts,
  testId: 'manual-sse-six-sessions', addCleanup: (_, callback) => cleanups.push(callback) }, { mode: 'core' });
const browser = await chromium.launch({ headless: true, args: ['--js-flags=--expose-gc', '--disable-dev-shm-usage'] });
const context = await browser.newContext({ viewport: { width: 2400, height: 1500 } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(String(error)));
const observation = await observePage(page, perf, context);
const cdp = await context.newCDPSession(page);
await cdp.send('Performance.enable');
const result = { run, boundary: '真实 Workbench/Gateway/Backend/SDK；model_stream HTTP transport精确SSE回放，原控制屏障逐批释放；无模型HTTP服务', samples: [], errors };
const barrier = (operation_id, index) => ({ operation_id, phase: `model.response_frame/${index}` });
const acknowledged = value => testControl({action: 'acknowledged', timeout:120, ...value});
async function api(path, init = {}) {
  return page.evaluate(async ({ path, init }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential,
      'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return body.data;
  }, { path, init });
}
async function command(id, ...args) {
  return page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
}
async function sample(label) {
  await cdp.send('HeapProfiler.collectGarbage');
  const metrics = Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(item => [item.name, item.value]));
  const dom = await page.locator('section.boxteam-thread').evaluateAll(elements => elements.map(element => {
    const shadow = element.querySelector('.boxteam-react-surface')?.shadowRoot;
    const shell = shadow?.querySelector('.chat-stream-virtual-shell');
    const scroller = shadow?.querySelector('[data-virtuoso-scroller]');
    const mount = shadow?.querySelector('.react-session-content');
    const rootKey = mount && Object.keys(mount).find(key => key.startsWith('__reactContainer$'));
    const root = rootKey && mount[rootKey].stateNode.current;
    let state;
    const visit = fiber => {
      if (!fiber || state) return;
      for (let hook = fiber.memoizedState; hook && 'memoizedState' in hook; hook = hook.next) {
        const value = hook.memoizedState;
        if (value?.messageStreamsByTurnStream instanceof Map) { state = value; break; }
      }
      visit(fiber.child); visit(fiber.sibling);
    };
    visit(root);
    const stream = state && [...state.messageStreamsByTurnStream.values()].at(-1);
    const answer = [...(shadow?.querySelectorAll('.chat-response-bubble') ?? [])].at(-1);
    const text = answer?.querySelector('.chat-markdown-bounded') ?? answer;
    const walker = text && document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
    let node;
    if (walker) for (let candidate = walker.nextNode(); candidate; candidate = walker.nextNode()) {
      if (candidate.textContent.trim()) node = candidate;
    }
    let tailVisible = false;
    if (node?.nodeType === Node.TEXT_NODE && node.textContent.length) {
      const range = document.createRange();
      range.setStart(node, Math.max(0,node.textContent.trimEnd().length - 8));
      range.setEnd(node,node.textContent.trimEnd().length);
      const rect = range.getBoundingClientRect();
      const clip = text.closest('.chat-markdown-bounded') ?? text;
      const bounds = clip.getBoundingClientRect();
      const outer = scroller?.getBoundingClientRect();
      tailVisible = rect.top >= bounds.top - 2 && rect.bottom <= bounds.bottom + 2
        && !!outer && rect.top >= outer.top && rect.bottom <= outer.bottom;
    }
    return { id: element.dataset.sessionId, turns: shell?.dataset.turnCount,
      nodes: shadow?.querySelectorAll('*').length, scrollTop: scroller?.scrollTop,
      scrollHeight: scroller?.scrollHeight, height: scroller?.clientHeight,
      streams: state?.messageStreamsByTurnStream.size, seq: stream?.lastEventSeq,
      blocks: stream?.blocks.length, textChars: stream?.blocks.reduce((sum, block) => sum + block.text.length, 0),
      pendingEventChars: state && [...state.pendingConversations.values()].flat().reduce((sum, pending) => sum + JSON.stringify(pending.events).length, 0),
      tailVisible,
      renderedTextChars: text?.textContent.length };
  }));
  const row = { label, at: Date.now(), heapUsed: metrics.JSHeapUsedSize, heapTotal: metrics.JSHeapTotalSize,
    nodes: metrics.Nodes, listeners: metrics.JSEventListeners, taskSeconds: metrics.TaskDuration, dom };
  result.samples.push(row);
  await observation.checkpoint(label);
  await writeFile(`${artifacts}/result.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(row));
  return row;
}

try {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('workspace-navigation-root-drop-target')).toBeVisible({ timeout: 60000 });
  const health = await api('/api/gateway/health');
  const active = (await api('/api/gateway/workspaces')).active_workspace;
  const headers = { 'X-BoxTeam-Workspace-Id': active.workspace_id, 'X-BoxTeam-Target-Gateway-Id': active.gateway_id };
  const workspace = await api('/api/v1/workspace', { headers });
  if (workspace.workspace_id !== active.workspace_id || health.gateway_id !== active.gateway_id) throw new Error('初始化 owner 不一致');
  result.owners = [];
  await command('vscode.setEditorLayout', { orientation: 0, groups: [{ groups: [{}, {}] }, { groups: [{}, {}] }, { groups: [{}, {}] }] });
  for (let index = 0; index < 6; index++) {
    const session = await api('/api/v1/sessions', { method: 'POST', headers,
      body: JSON.stringify({ title: `${run} 内存会话 ${index + 1}`, agent_id: 'default' }) });
    const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, { headers });
    const owner = { gatewayId: active.gateway_id, workspaceId: active.workspace_id, sessionId: session.session_id, threadId: main.thread_id };
    result.owners.push(owner);
    await planChatCalls(owner.sessionId, owner.threadId, [{ label: `${run}-history-${index}`, text: '这是可控 SSE 的完整回复。\n'.repeat(60) }]);
    await command('_workbench.open', `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${owner.sessionId}/${owner.threadId}`, [index, { pinned: true }]);
    const pane = page.locator(`section.boxteam-thread[data-session-id="${owner.sessionId}"]`);
    await expect(pane.getByTestId('composer-input')).toBeVisible({ timeout: 60000 });
    await pane.getByTestId('composer-input').fill('生成一些历史内容，用于持续输出时向上阅读。');
    await pane.getByTestId('composer-send').click();
    await expect(pane.getByText('这是可控 SSE 的完整回复。', { exact: false }).first()).toBeVisible({ timeout: 60000 });
    await expect(pane.getByRole('button', { name: '停止', exact: true })).toHaveCount(0, { timeout: 30000 });
  }
  for (const owner of result.owners) {
    const [callId] = await testControl({ action: 'plan_chat', session_id: owner.sessionId, thread_id: owner.threadId,
      responses: [{ response_id: `${run}-${owner.sessionId}`, deltas: [ { role: 'assistant' },
        ...Array.from({length:1500}, (_, index) => ({content: `${Math.floor(index / 250)}:${index % 250} ` + '持续输出用于观察内存与交互。'.repeat(24) + '\n'}))], finish_reason: 'stop' }] });
    owner.callId = callId;
    for (const index of [0, 251, 501, 751, 1001, 1251, 1501]) {
      await testControl({action: 'arm', ...barrier(callId, index)});
    }
    const pane = page.locator(`section.boxteam-thread[data-session-id="${owner.sessionId}"]`);
    await pane.getByTestId('composer-input').fill('持续输出，保持运行直到手动结束。');
    await pane.getByTestId('composer-send').click();
  }
  await Promise.all(result.owners.map(owner => acknowledged(barrier(owner.callId, 0))));
  await sample('started');
  for (let round = 0; round < 6; round++) {
    await Promise.all(result.owners.map(owner => testControl({action:'release', ...barrier(owner.callId, round === 0 ? 0 : round * 250 + 1)})));
    const typingStarted = Date.now();
    const composer = page.locator(`section.boxteam-thread[data-session-id="${result.owners[0].sessionId}"]`).getByTestId('composer-input');
    await composer.fill(`持续生成期间输入草稿 ${round}`);
    await expect(composer).toHaveValue(`持续生成期间输入草稿 ${round}`, {timeout:3000});
    result.inputLatencies ??= [];
    result.inputLatencies.push(Date.now() - typingStarted);
    await Promise.all(result.owners.map(owner => acknowledged(barrier(owner.callId, (round+1) * 250 + 1))));
    await expect.poll(async () => {
      const text = await page.locator('section.boxteam-thread .chat-markdown-lightweight').allTextContents();
      return text.filter(value => value.includes(`${round}:249 `)).length;
    }, {timeout:60000}).toBe(round === 0 ? 6 : 4);
    if (round === 0) {
      for (const owner of result.owners.slice(0, 2)) {
        const scroll = page.locator(`section.boxteam-thread[data-session-id="${owner.sessionId}"] [data-virtuoso-scroller]`);
        await scroll.hover();
        await page.mouse.wheel(0, -100000);
      }
    }
    const row = await sample(`wave-${round + 1}`);
    expect(row.dom.slice(0, 2).every(item => item.scrollTop <= 2)).toBe(true);
    if (!baseline) expect(row.dom.slice(2).every(item => item.tailVisible)).toBe(true);
  }
  await page.screenshot({ path: `${artifacts}/six-sessions.png` });
  await Promise.all(result.owners.map(owner => testControl({action:'release', ...barrier(owner.callId,1501)})));
  await expect(page.locator('#interruptButton')).toHaveCount(0, { timeout: 60000 });
  const completed = await sample('completed');
  expect(completed.dom.slice(0, 2).every(item => item.scrollTop <= 2)).toBe(true);
  if (!baseline) expect(completed.dom.slice(2).every(item => item.tailVisible)).toBe(true);
  for (const owner of result.owners.slice(0, 2)) {
    const pane = page.locator(`section.boxteam-thread[data-session-id="${owner.sessionId}"]`);
    await pane.getByRole('button', {name:'跳到最新消息',exact:true}).click();
    await expect(pane.getByTestId('composer-input')).toBeVisible();
  }
  expect(errors).toEqual([]);
  result.completed = true;
} catch (error) {
  result.failure = String(error.stack ?? error);
  await page.screenshot({ path: `${artifacts}/failure.png` }).catch(() => {});
  throw error;
} finally {
  await writeFile(`${artifacts}/result.json`, JSON.stringify(result, null, 2));
  for (const owner of result.owners ?? []) {
    if (!owner.callId) continue;
    for (const index of [0,251,501,751,1001,1251,1501]) {
      await testControl({action:'release', ...barrier(owner.callId,index)});
    }
  }
  await observation.close();
  for (const cleanup of cleanups) await cleanup();
  await browser.close();
}
