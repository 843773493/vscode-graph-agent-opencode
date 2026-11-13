// Integration：SSH 执行与隧道由既有 loopback 装具替换，其余 owner、HTTP 和 Workbench 均为产品。
import { writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../../support/e2e_control/browser_client.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_REMOTE_URI_FIXTURE);
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_remote_gateway_uri.py');
const host = createInterface({ input: process.stdin });
const responses = host[Symbol.asyncIterator]();
run.addCleanup('远端 URI 进程协调', () => host.close());
const hostAction = async action => {
  process.stdout.write(`BOXTEAM_REMOTE_ACTION ${action}\n`);
  const response = await responses.next();
  if (response.done) throw new Error(`远端 URI 协调未返回 ${action}`);
  const receipt = JSON.parse(response.value);
  expect(receipt.action).toBe(action);
  return receipt;
};
const result = { phase: 'initialize', scenario: fixture.scenario, classification: 'Integration', pageErrors: [], consoleErrors: [], requests: [],
  addresses: [], bodyReads: [], remoteBodyReceipts: [], rejectedReads: [], forbiddenWrites: [], networkEvents: [],
  boundary: '真实控制 Gateway A、远端 Gateway B 及独立 Backend；两者同 workspace UUID。SSH 配对执行和隧道是既有 loopback 替身；Backend 沿既有受控 ID／时钟测试 composition，业务与 owner 不替换。'
    + (fixture.scenario === 'resources'
      ? '模型统一精确 model_stream。仅一份真实地址解析回执在浏览器 HTTP 边界延迟；不替换地址、正文或 owner。旧 B 端口重用为另一真实 Gateway 后端 A，由原连接入口核拒绝。'
      : '模型零调用。CDP 与独立 socket 仅作连接容量诊断对照，页面同源 fetch 必须成功，不用独立连接继续业务。') };
const pending = [], receiptErrors = [], records = new WeakMap();
let page, failure, releaseParse, parseSeen, holdParse = false;
const held = new Promise(resolve => { parseSeen = resolve; });
const released = new Promise(resolve => { releaseParse = resolve; });
const phase = async value => { result.phase = value; await writeFile(fixture.resultPath, JSON.stringify(result, null, 2) + '\n'); };

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('远端 URI Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1750, height: 1050 }, permissions: ['clipboard-read', 'clipboard-write'] });
  run.addCleanup('远端 URI BrowserContext', () => context.close());
  page = await context.newPage(); page.setDefaultTimeout(20_000);
  run.addCleanup('远端 URI 截图', () => page.screenshot({ path: fixture.screenshotPath, fullPage: false }));
  const network = new Map(), cdp = await context.newCDPSession(page);
  const networkEvent = (event, data) => result.networkEvents.push({ event, phase: result.phase, ...data });
  await cdp.send('Network.enable');
  cdp.on('Network.requestWillBeSent', event => {
    if (!new URL(event.request.url).pathname.startsWith('/api/')) return;
    const request = { requestId: event.requestId, loaderId: event.loaderId, url: event.request.url, method: event.request.method,
      timestamp: event.timestamp, phase: result.phase, type: event.type, initiator: event.initiator.type };
    network.set(event.requestId, request); networkEvent('request', request);
  });
  cdp.on('Network.responseReceived', event => {
    const request = network.get(event.requestId);
    if (!request) return;
    const response = event.response;
    request.response = { status: response.status, protocol: response.protocol, connectionId: response.connectionId,
      connectionReused: response.connectionReused, remotePort: response.remotePort, mimeType: response.mimeType,
      httpRequestId: response.headers['x-request-id'] ?? response.headers['X-Request-ID'], timing: response.timing };
    networkEvent('response', { requestId: event.requestId, timestamp: event.timestamp, ...request.response });
  });
  cdp.on('Network.loadingFinished', event => {
    if (!network.has(event.requestId)) return;
    network.get(event.requestId).terminal = 'finished'; networkEvent('finished', event);
  });
  cdp.on('Network.loadingFailed', event => {
    if (!network.has(event.requestId)) return;
    network.get(event.requestId).terminal = 'failed'; networkEvent('failed', event);
  });
  page.on('close', () => networkEvent('page-closed', {}));
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push({ phase: result.phase, text: message.text() }); });
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return;
    const headers = request.headers(), method = request.method();
    const observed = { phase: result.phase, path, method, gatewayId: headers['x-boxteam-target-gateway-id'],
      workspaceId: headers['x-boxteam-workspace-id'], body: ['GET', 'HEAD'].includes(method) ? undefined : request.postDataJSON() };
    result.requests.push(observed); records.set(request, observed);
    if (path.startsWith('/api/gateway/resource-addresses/')) result.addresses.push(observed);
    observed.bodyRead = /\/workspace\/files\/(raw|content)$/.test(path)
      || path === '/api/v1/resources/navigation' && observed.body?.operation === 'read'
      || method === 'GET' && /^\/api\/v1\/sessions\/[^/]+\/(threads\/[^/]+\/)?(bootstrap|history|messages|context)(\/|$)/.test(path);
    if (observed.bodyRead) result.bodyReads.push(observed);
    if (result.phase !== 'prepare-history' && method !== 'GET' && method !== 'HEAD'
      && (/\/(messages|text-mutations|untrack)(\/|$)/.test(path)
        || /\/api\/gateway\/workspaces\/[^/]+\/(start|activate|connect|reconnect|stop)$/.test(path))) result.forbiddenWrites.push(observed);
  });
  page.on('requestfinished', request => {
    const observed = records.get(request);
    if (!observed || !observed.bodyRead && !observed.path.startsWith('/api/gateway/resource-addresses/')) return;
    pending.push((async () => {
      const response = await request.response();
      const receipt = { status: response.status(), requestId: response.headers()['x-request-id'] };
      if (observed.path.startsWith('/api/gateway/resource-addresses/') || observed.bodyRead && !response.ok()) {
        const envelope = await response.json();
        expect(envelope.request_id).toBe(receipt.requestId); receipt.data = envelope.data; receipt.detail = envelope.detail;
      }
      observed.receipt = receipt;
      if (observed.bodyRead && observed.gatewayId === fixture.gatewayB && response.ok()) {
        result.remoteBodyReceipts.push({ ...observed, ...receipt });
      }
    })().catch(error => receiptErrors.push(String(error))));
  });
  await page.route('**/api/gateway/resource-addresses/parse', async route => {
    if (!holdParse || !route.request().postDataJSON().uri.includes(fixture.gatewayB)) return route.continue();
    holdParse = false;
    const response = await route.fetch();
    result.delayedAddress = { status: response.status(), response: await response.json() };
    parseSeen(); await released; await route.fulfill({ response });
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential', { signal: AbortSignal.timeout(15_000) })).json()).data.token;
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', signal: AbortSignal.timeout(60_000),
      headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json();
    if (!response.ok || !payload.request_id || payload.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, payload }));
    }
    return payload.data;
  }, { path, owner, body });
  const a = { gatewayId: fixture.gatewayA, workspaceId: fixture.workspaceId }, b = { ...a, gatewayId: fixture.gatewayB };
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const groups = page.locator('.editor-group-container');
  const text = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true }).locator('.view-lines');
  const ready = async () => {
    await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
    const loaderId = (await cdp.send('Page.getFrameTree')).frameTree.frame.loaderId;
    // 导航先挂载；本次文档的原命令订阅只会在 Gateway 身份发布后建立。
    await expect.poll(() => [...network.values()].some(item => item.loaderId === loaderId && !item.terminal
      && item.url.endsWith('/api/gateway/service-plugins/commands/events') && item.response?.status === 200),
    { timeout: 30_000 }).toBe(true);
  };
  const scopedRow = owner => quick.locator('.monaco-list-row').filter({ hasText: owner.gatewayId });
  const chooseB = async button => {
    await quick.getByRole('button', { name: button, exact: true }).click();
    await expect(quick).toContainText('选择快速访问搜索范围');
    await scopedRow(a).getByRole('checkbox').uncheck();
    await scopedRow(b).getByRole('checkbox').check();
    await quick.getByRole('button', { name: '确定', exact: true }).click();
  };
  const focusPick = async row => {
    await expect(row).toBeVisible(); await input.focus();
    const size = Number(await row.getAttribute('aria-setsize'));
    for (let index = 0; index <= size; index++) {
      if ((await row.getAttribute('class'))?.split(/\s+/).includes('focused')) return;
      await page.keyboard.press('ArrowDown');
    }
    await expect(row).toHaveClass(/(?:^|\s)focused(?:\s|$)/);
  };
  const open = async (uri, side = false) => {
    await command('workbench.action.quickOpen', uri); await expect(input).toHaveValue(uri);
    await input.press(side ? 'Alt+Enter' : 'Enter'); await expect(quick).not.toBeVisible();
  };
  const copyPick = async (row, expected) => {
    await row.hover(); const tabs = await page.locator('.tab').count();
    await row.getByRole('button', { name: '复制资源 URI', exact: true }).click();
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(expected);
    expect(result.bodyReads.filter(item => item.phase === result.phase)).toEqual([]);
    await expect(page.locator('.tab')).toHaveCount(tabs); await expect(input).toBeFocused();
  };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' }); await ready();
  const health = await api('/api/gateway/health'), registry = await api('/api/gateway/workspaces?check_health=false');
  expect(health.gateway_id).toBe(a.gatewayId); expect(registry.active_workspace).toEqual({ gateway_id: a.gatewayId, workspace_id: a.workspaceId });
  expect(registry.items.filter(item => item.workspace_id === a.workspaceId).map(item => item.gateway_id).sort()).toEqual([a.gatewayId, b.gatewayId].sort());
  expect((await api('/api/v1/workspace', a)).workspace_id).toBe(a.workspaceId);
  expect((await api('/api/v1/workspace', b)).workspace_id).toBe(b.workspaceId);
  result.initialization = { health, active: registry.active_workspace, owners: [a, b] };
  await command('workbench.action.closeAllEditors');
  const publicFile = owner => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/same-uri.txt`;
  const publicSource = `boxteam://${b.gatewayId}/workspace/${b.workspaceId}/resources/agent-spec/root/AGENTS.md`;
  if (fixture.scenario === 'connection-capacity') {
    await phase('connection-capacity-open-two-owners');
    await open(publicFile(a)); await expect(text(0)).toContainText('REMOTE-TEXT-A');
    await open(publicFile(b), true); await expect(text(1)).toContainText('REMOTE-TEXT-B');
    await expect.poll(() => [...network.values()].filter(item => !item.terminal
      && item.url.includes('/workspace/observations/stream') && item.response?.status === 200).length).toBe(2);
    await phase('connection-capacity-probe');
    result.activeBeforeProbe = structuredClone([...network.values()].filter(item => !item.terminal));
    // 独立连接只作诊断对照，产品请求仍必须经页面的同源 fetch 完成。
    const started = performance.now();
    const credential = await context.request.get(`${fixture.baseUrl}/api/gateway/auth/local-credential`, { timeout: 5_000 });
    expect(credential.ok()).toBe(true);
    const token = (await credential.json()).data.token;
    const independent = await context.request.get(`${fixture.baseUrl}/api/gateway/health`, {
      headers: { 'X-Local-Token': token }, timeout: 5_000,
    });
    const independentBody = await independent.json();
    expect(independentBody.request_id).toBe(independent.headers()['x-request-id']);
    result.independentProbe = { status: independent.status(), milliseconds: performance.now() - started,
      requestId: independentBody.request_id, gatewayId: independentBody.data.gateway_id };
    expect(result.independentProbe.status).toBe(200);
    expect(result.independentProbe.gatewayId).toBe(a.gatewayId);
    result.browserProbe = await page.evaluate(async token => {
      const started = performance.now();
      try {
        const response = await fetch('/api/gateway/health', {
          headers: { 'X-Local-Token': token }, signal: AbortSignal.timeout(5_000),
        });
        const body = await response.json();
        return { ok: response.ok, status: response.status, requestId: body.request_id,
          header: response.headers.get('X-Request-ID'), gatewayId: body.data?.gateway_id, milliseconds: performance.now() - started };
      } catch (error) {
        return { ok: false, name: error.name, message: error.message, milliseconds: performance.now() - started };
      }
    }, token);
    const probeRequests = () => [...network.values()].filter(item => item.phase === 'connection-capacity-probe'
      && item.url.endsWith('/api/gateway/health'));
    await expect.poll(() => probeRequests().length).toBe(1);
    await expect.poll(() => probeRequests().every(item => item.terminal)).toBe(true);
    result.activeAfterProbe = structuredClone([...network.values()].filter(item => !item.terminal));
    expect(result.browserProbe.ok).toBe(true);
    expect(result.browserProbe.requestId).toBe(result.browserProbe.header);
    expect(result.browserProbe.gatewayId).toBe(a.gatewayId); result.connectionCapacity = true;
  } else {
  const filePick = quick.getByRole('option').filter({ has: page.locator(`[data-quick-input-id^="boxteam-file://${b.gatewayId}/"]`) }).filter({ hasText: 'same-uri.txt' });
  const sourcePick = quick.getByRole('option', { name: new RegExp(`^AGENTS\\.md, .*${b.gatewayId} · ${b.workspaceId} · `) });

  await phase('select-and-copy-remote-file');
  await command('workbench.action.quickOpen', 'same-uri.txt');
  await chooseB('选择搜索范围'); await expect(filePick).toHaveCount(1);
  await copyPick(filePick, publicFile(b));
  await focusPick(filePick); await input.press('Enter');
  await expect(text(0)).toContainText('REMOTE-TEXT-B'); await expect(text(0)).not.toContainText('REMOTE-TEXT-A');
  result.remoteText = true;
  await phase('select-and-copy-remote-source');
  await command('boxteam.quickAccess.resources', a); await chooseB('选择来源所属工作区');
  await copyPick(sourcePick, publicSource); await focusPick(sourcePick); await input.press('Enter');
  await expect(text(0)).toContainText('REMOTE-AGENTS-B');
  await text(0).click(); await page.keyboard.type('MUST-NOT-WRITE');
  await expect(text(0)).not.toContainText('MUST-NOT-WRITE'); result.remoteReadonlySource = true;
  await phase('side-source-and-late-options'); await open(publicSource, true);
  await expect(groups).toHaveCount(2); await expect(text(1)).toContainText('REMOTE-AGENTS-B');
  holdParse = true; await command('workbench.action.quickOpen', publicFile(b)); await input.press('Enter'); await held;
  await command('workbench.action.focusFirstEditorGroup'); await command('_workbench.open', publicFile(a));
  await expect(text(0)).toContainText('REMOTE-TEXT-A'); releaseParse();
  await expect(text(1)).toContainText('REMOTE-TEXT-B'); await expect(groups).toHaveCount(2);
  expect(result.delayedAddress.status).toBe(200); expect(result.delayedAddress.response.data.gateway_id).toBe(b.gatewayId);
  result.lateOpenKeptOwnerAndGroup = true;

  await phase('prepare-history');
  const sameTitle = '跨 Gateway 同名 Session';
  const localSession = await api('/api/v1/sessions', a, { title: sameTitle });
  const session = await api('/api/v1/sessions', b, { title: sameTitle });
  expect(localSession.session_id).not.toBe(session.session_id);
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, b);
  await testControl({ action: 'plan_model', session_id: session.session_id, thread_id: main.thread_id, interactions: [fixture.interaction] });
  const accepted = await api(`/api/v1/sessions/${session.session_id}/threads/${main.thread_id}/messages`, b,
    { message: { content: '仅 B 生成真实 main 与 child 的来源历史。' }, run: {} });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`, b);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 45_000 }).toBe('completed');
  const point = await api(`/api/v1/sessions/${session.session_id}/fork-source-point`, b);
  const fork = await api(`/api/v1/sessions/${session.session_id}/fork-context`, b, {
    idempotency_key: `remote-uri-child-${session.session_id}`, source_uri: point.source_uri,
    source_view_id: point.source_view_id, source_view_revision: point.source_view_revision,
    anchor_item_id: point.anchor_item_id, target_container_uri: point.child_container_uri,
    mode: 'view_prefix_fork', creation_kind: 'new_child_thread',
  });
  expect(fork.session.session_id).toBe(session.session_id); expect(fork.thread_id).not.toBe(main.thread_id);
  result.session = { localSessionId: localSession.session_id, sessionId: session.session_id, main: main.thread_id, child: fork.thread_id };
  const sessionUri = `boxteam://${b.gatewayId}/workspace/${b.workspaceId}/resources/session/${session.session_id}`;
  const threadUri = id => `${sessionUri}/threads/${id}`;
  const pane = id => page.locator(`section.boxteam-thread[data-workspace-id="${b.workspaceId}"][data-session-id="${session.session_id}"][data-thread-id="${id}"]`);
  await phase('open-remote-main-and-child');
  await command('workbench.action.closeAllEditors'); await command('workbench.action.joinAllGroups');
  await open(sessionUri); await expect(pane(main.thread_id).getByTestId('composer-input')).toBeEnabled({ timeout: 30_000 });
  await pane(main.thread_id).getByTestId('composer-input').fill('B MAIN DRAFT');
  await open(threadUri(fork.thread_id), true); await expect(pane(fork.thread_id).getByTestId('composer-input')).toBeEnabled({ timeout: 30_000 });
  await pane(fork.thread_id).getByTestId('composer-input').fill('B CHILD DRAFT');
  await expect(pane(main.thread_id)).toContainText('已创建用于 Thread 引用的产品历史。');
  await expect(pane(fork.thread_id)).toContainText('已创建用于 Thread 引用的产品历史。');
  await expect(groups).toHaveCount(2); await expect(pane(main.thread_id).getByTestId('composer-input')).toHaveValue('B MAIN DRAFT');
  result.exactMainChild = true;
  for (const id of [main.thread_id, fork.thread_id]) {
    await phase(`copy-thread-${id}`); await command('workbench.action.quickOpen', '');
    const row = quick.getByRole('option').filter({ has: page.locator(`[data-quick-input-id="boxteam-thread://${b.gatewayId}/${b.workspaceId}/${session.session_id}/${id}"]`) });
    await copyPick(row, threadUri(id)); await input.press('Escape');
  }
  result.copyWithoutBody = true;
  await phase('normal-restore');
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  await expect(groups).toHaveCount(2);
  await expect(pane(main.thread_id).getByTestId('composer-input')).toHaveValue('B MAIN DRAFT', { timeout: 30_000 });
  await expect(pane(fork.thread_id).getByTestId('composer-input')).toHaveValue('B CHILD DRAFT', { timeout: 30_000 });
  result.normalRestore = true;
  await command('workbench.action.closeAllEditors'); await command('workbench.action.joinAllGroups');
  await phase('remote-stopped'); result.stop = await hostAction('stop');
  await expect.poll(async () => (await api('/api/gateway/workspaces')).items.find(item => item.gateway_id === b.gatewayId)?.status,
    { timeout: 30_000 }).toBe('offline');
  const rejectSource = async () => {
    await command('notifications.clearAll');
    const before = result.bodyReads.length, groupCount = await groups.count();
    await open(publicSource, true);
    const detail = `目标 Gateway 当前没有可用 channel: ${b.gatewayId}`;
    await expect(page.locator('.notification-list-item-message').filter({ hasText: detail })).toBeVisible();
    await expect(groups).toHaveCount(groupCount); await expect(page.locator('.tab').filter({ hasText: 'AGENTS.md' })).toHaveCount(0);
    const reads = result.bodyReads.slice(before);
    expect(reads).toHaveLength(1);
    expect(reads[0]).toEqual(expect.objectContaining({ path: '/api/v1/resources/navigation', method: 'POST',
      gatewayId: b.gatewayId, workspaceId: b.workspaceId, body: expect.objectContaining({ uri: publicSource, operation: 'read' }) }));
    await expect.poll(() => reads[0].receipt?.status).toBe(409);
    expect(reads[0].receipt.detail).toEqual({ code: 'federation-channel-closed', message: detail, detail: {} });
    result.rejectedReads.push(structuredClone(reads[0]));
    expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace).toEqual({ gateway_id: a.gatewayId, workspace_id: a.workspaceId });
  };
  await rejectSource(); result.offlineRejected = true;
  await phase('old-route-reused'); result.reusedRoute = await hostAction('reuse');
  expect(result.reusedRoute.status).toBe(502); expect(result.reusedRoute.response.request_id).toBe(result.reusedRoute.header);
  await rejectSource(); result.reusedRouteRejected = true;
  await phase('explicit-new-route'); result.rebound = await hostAction('rebind');
  expect(result.rebound.target.gateway_id).toBe(b.gatewayId); expect(result.rebound.target.workspace_id).toBe(b.workspaceId);
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  await open(publicSource); await expect(text(0)).toContainText('REMOTE-AGENTS-B');
  await open(publicFile(b)); await expect(text(0)).toContainText('REMOTE-TEXT-B');
  await open(sessionUri); await expect(pane(main.thread_id).getByTestId('composer-input')).toHaveValue('B MAIN DRAFT', { timeout: 30_000 });
  await open(threadUri(fork.thread_id), true);
  await expect(pane(fork.thread_id).getByTestId('composer-input')).toHaveValue('B CHILD DRAFT', { timeout: 30_000 });
  result.newRouteReopened = true;
  expect(result.addresses.filter(item => item.phase.startsWith('copy-thread-')).map(item => item.body)).toEqual([
    { local_gateway_id: a.gatewayId, resource_gateway_id: b.gatewayId, workspace_id: b.workspaceId, session_id: session.session_id, thread_id: main.thread_id },
    { local_gateway_id: a.gatewayId, resource_gateway_id: b.gatewayId, workspace_id: b.workspaceId, session_id: session.session_id, thread_id: fork.thread_id },
  ]);
  }
  await Promise.all(pending); expect(receiptErrors).toEqual([]);
  expect(result.remoteBodyReceipts.length).toBeGreaterThan(0);
  expect(result.forbiddenWrites).toEqual([]); expect(result.pageErrors).toEqual([]);
  await phase('complete');
} catch (error) { failure = error; result.error = String(error.stack ?? error); }
finally {
  releaseParse();
  try { await run.close(); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '远端 URI 验收与资源清理均失败') : cause; }
  if (failure) result.error = String(failure.stack ?? failure);
  try { await writeFile(fixture.resultPath, JSON.stringify(result, null, 2) + '\n'); }
  catch (cause) { failure = failure ? new AggregateError([failure, cause], '远端 URI 验收与结果保存均失败') : cause; }
}
if (failure) throw failure;
