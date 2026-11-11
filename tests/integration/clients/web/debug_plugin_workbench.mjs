import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { testControl } from '../../../support/e2e_control/browser_client.mjs';
import { installPlugin } from '../../../support/plugin_package.mjs';

const input = JSON.parse(process.env.BOXTEAM_DEBUG_UI_INPUT);
const result = { phase: 'initialization', pageErrors: [], dap: [] };
const errors = [];
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  let loseEvaluation = false;
  let lostEvaluationRequests = 0;
  // 只丢弃真实服务已经提交的求值回执；不替代执行、结果或状态。
  await page.routeWebSocket('**/api/v1/debug/node/runs/*/dap*', socket => {
    const server = socket.connectToServer();
    const channel = new URL(socket.url()).pathname;
    let lostSequence;
    socket.onMessage(raw => {
      const value = JSON.parse(raw.toString());
      result.dap.push({ direction: 'out', channel, value });
      if (loseEvaluation && value.command === 'evaluate' && value.arguments?.expression === 'state.counter += 1') {
        lostSequence = value.seq;
        lostEvaluationRequests++;
      }
      server.send(raw);
    });
    server.onMessage(raw => {
      const value = JSON.parse(raw.toString());
      if (lostSequence !== undefined && value.type === 'response' && value.request_seq === lostSequence) {
        result.responseLoss = { response: value, requestCount: lostEvaluationRequests };
        socket.close({ code: 1011, reason: 'integration_evaluate_response_lost' });
        server.close({ code: 1000, reason: 'integration_response_lost' });
        return;
      }
      result.dap.push({ direction: 'in', channel, value });
      socket.send(raw);
    });
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const api = async (path, options = {}, owner) => page.evaluate(async ({ path, options, owner }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId,
        'X-BoxTeam-Workspace-Id': owner.workspaceId } : {}) }, body: options.body && JSON.stringify(options.body) });
    const value = await response.json();
    if (!response.ok || value.code !== 0 || value.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(`${path}: ${response.status} ${JSON.stringify(value)}`);
    }
    return value.data;
  }, { path, options, owner });
  result.initialization = [];
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    result.initialization.push({ path, data: await api(path) });
  }
  const active = result.initialization[1].data.active_workspace;
  const owner = { gatewayId: active.gateway_id, workspaceId: active.workspace_id };
  result.owner = owner;
  const bindDebugger = async (target, version, idempotencyKey) => {
    const path = `/api/v1/workspaces/${target.workspaceId}/config/sources/workspace_root_mutable_override`;
    const source = await api(path, {}, target), document = JSON.parse(source.jsonc);
    document.plugins = { ...document.plugins, runtime: { entries: version === null ? [] : [{
      package_id: 'boxteam.developer-tools', package_version: version, plugin_id: 'debugging', enabled: true,
    }] } };
    const saved = await api(path, { method: 'PUT', body: { gateway_id: target.gatewayId, workspace_id: target.workspaceId,
      source_key: 'workspace_root_mutable_override', jsonc: JSON.stringify(document), base_file_digest: source.file_digest,
      base_layer_revision: source.layer_revision, base_layer_digest: source.layer_digest,
      expected_active_revision: source.active_revision, expected_active_digest: source.active_digest,
      idempotency_key: idempotencyKey } }, target);
    assert.equal(saved.edit.state, 'active');
  };
  await bindDebugger(owner, '0.1.0', 'debug-ui-enable');
  let ready;
  await expect.poll(async () => {
    const snapshot = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: owner.gatewayId } });
    ready = snapshot.plugins.find(item => item.binding.plugin_id === 'debugging' && item.binding.workspace_id === owner.workspaceId);
    if (ready?.state === 'failed') throw new Error(`Debug 插件启动失败: ${JSON.stringify(ready)}`);
    return ready?.state;
  }, { timeout: 45_000 }).toBe('ready');
  result.instance = ready.instance;
  const session = await api('/api/v1/sessions', { method: 'POST', body: { title: 'Debug UI shared run' } }, owner);
  const thread = await api(`/api/v1/sessions/${session.session_id}/main-thread`, {}, owner);
  Object.assign(owner, { sessionId: session.session_id, threadId: thread.thread_id });
  const registered = await api('/api/gateway/managed-workspaces', { method: 'POST', body: {
    root_path: input.siblingWorkspace, name: 'Debug sibling workspace', create_directory: false,
  } });
  const sibling = registered.items.find(item => item.gateway_id === owner.gatewayId && item.name === 'Debug sibling workspace');
  assert(sibling); assert.notEqual(sibling.workspace_id, owner.workspaceId);
  const otherOwner = { gatewayId: owner.gatewayId, workspaceId: sibling.workspace_id };
  await bindDebugger(otherOwner, '0.1.0', 'debug-ui-enable-sibling');
  let siblingReady;
  await expect.poll(async () => {
    const snapshot = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: owner.gatewayId } });
    siblingReady = snapshot.plugins.find(item => item.binding.plugin_id === 'debugging' && item.binding.workspace_id === otherOwner.workspaceId);
    if (siblingReady?.state === 'failed') throw new Error(`兄弟工作区Debug启动失败: ${JSON.stringify(siblingReady)}`);
    return siblingReady?.state;
  }, { timeout: 45_000 }).toBe('ready');
  assert.notEqual(siblingReady.instance.instance_id, ready.instance.instance_id);
  const otherSession = await api('/api/v1/sessions', { method: 'POST', body: { title: 'Debug focus destination' } }, otherOwner);
  const otherThread = await api(`/api/v1/sessions/${otherSession.session_id}/main-thread`, {}, otherOwner);
  Object.assign(otherOwner, { sessionId: otherSession.session_id, threadId: otherThread.thread_id });
  await api('/api/v1/debug/node/configurations', { method: 'POST', body: { session_id: owner.sessionId, thread_id: owner.threadId,
    name: 'UI shared Node', script_path: 'debug-ui.mjs', working_directory: '.', args: [], breakpoints: [] } }, owner);
  await api('/api/v1/debug/node/configurations', { method: 'POST', body: { session_id: otherOwner.sessionId, thread_id: otherOwner.threadId,
    name: 'Other Thread Node', script_path: 'debug-ui.mjs', working_directory: '.', args: [], breakpoints: [] } }, otherOwner);
  const state = () => api(`/api/v1/debug/node?${new URLSearchParams({ session_id: owner.sessionId, thread_id: owner.threadId })}`, {}, owner);
  const otherState = () => api(`/api/v1/debug/node?${new URLSearchParams({ session_id: otherOwner.sessionId,
    thread_id: otherOwner.threadId })}`, {}, otherOwner);
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    let timer;
    try {
      return await Promise.race([globalThis.workbench.commands.executeCommand(id, ...args),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`工作台命令未完成: ${id}`)), 30_000); })]);
    } finally { clearTimeout(timer); }
  }, { id, args });
  const sessionRow = target => page.getByTestId(`catalog-node-${JSON.stringify([target.gatewayId, target.workspaceId])}-${target.sessionId}`)
    .locator(`button[data-session-id="${target.sessionId}"]`);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await sessionRow(owner).dblclick();
  const groups = page.locator('.editor-group-container');
  const threadPane = groups.first().locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(threadPane.getByTestId('composer-input')).toBeVisible();
  await threadPane.getByTestId('composer-input').click();
  await command('workbench.action.splitEditorRight');
  await expect(groups).toHaveCount(2);
  await command('workbench.action.focusSecondEditorGroup');
  await expect(groups.nth(1)).toHaveClass(/\bactive\b/);
  await command('_workbench.open', `boxteam-thread://${otherOwner.gatewayId}/${otherOwner.workspaceId}/${otherOwner.sessionId}/${otherOwner.threadId}`, [undefined, { pinned: true }]);
  const otherPane = groups.nth(1).locator(`section.boxteam-thread[data-session-id="${otherOwner.sessionId}"][data-thread-id="${otherOwner.threadId}"]`);
  await expect(threadPane.getByTestId('composer-input')).toBeVisible();
  await expect(otherPane.getByTestId('composer-input')).toBeVisible();
  await threadPane.getByTestId('composer-input').click();
  result.phase = 'editor-breakpoint';
  const roots = await api('/api/v1/workspace/files/roots', {}, owner);
  const quick = page.locator('.quick-input-widget');
  await command('workbench.action.quickOpen', roots.workspace_uri + '/debug-ui.mjs');
  await quick.getByText('打开链接', { exact: true }).click();
  await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true }).locator('.view-lines')).toContainText('const state = { counter: 1 }');
  await command('workbench.action.gotoLine');
  await quick.locator('input').first().fill(':2'); await quick.locator('input').first().press('Enter');
  await expect(quick).not.toBeVisible();
  await expect(page.getByRole('button', { name: /^Ln 2, Col/ })).toBeVisible();
  await command('editor.debug.action.toggleBreakpoint');
  await expect(page.locator('.glyph-margin-widgets [class*="codicon-debug-breakpoint"]')).toHaveCount(1);
  const siblingRoots = await api('/api/v1/workspace/files/roots', {}, otherOwner);
  await command('workbench.action.quickOpen', siblingRoots.workspace_uri + '/debug-ui.mjs');
  await quick.getByText('打开链接', { exact: true }).click();
  await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true }).locator('.view-lines')).toContainText('const state = { counter: 1 }');
  await command('workbench.action.gotoLine');
  await quick.locator('input').first().fill(':2'); await quick.locator('input').first().press('Enter');
  await expect(quick).not.toBeVisible();
  await expect(page.getByRole('button', { name: /^Ln 2, Col/ })).toBeVisible();
  await command('editor.debug.action.toggleBreakpoint');
  await expect(page.locator('.glyph-margin-widgets [class*="codicon-debug-breakpoint"]')).toHaveCount(1);
  await command('workbench.action.focusFirstEditorGroup');
  await sessionRow(owner).dblclick();
  await expect(threadPane.getByTestId('composer-input')).toBeVisible();
  await threadPane.getByTestId('composer-input').click();
  result.phase = 'stale-launch-after-version-change';
  const originalPlugin = ready;
  const originalDeclaration = (await api(`/api/v1/debug/node/debugger-contributions?workspace_id=${owner.workspaceId}`, {}, owner)).debuggers[0];
  await installPlugin(api, { gateway_id: owner.gatewayId, path: input.upgradedPackage });
  let releaseStaleConfiguration, enterStaleConfiguration;
  const staleBarrier = new Promise(resolve => { releaseStaleConfiguration = resolve; });
  const staleEntered = new Promise(resolve => { enterStaleConfiguration = resolve; });
  const stalePattern = url => url.pathname === '/api/v1/debug/node/configurations'
    && url.searchParams.get('session_id') === owner.sessionId && url.searchParams.get('thread_id') === owner.threadId;
  const delayStaleConfiguration = async route => { enterStaleConfiguration(); await staleBarrier; await route.continue(); };
  await page.route(stalePattern, delayStaleConfiguration);
  // 原命令可由 DebugService 报错后返回 false，也可拒绝；业务入口的明确拒绝才是准入证据。
  const staleLaunching = command('boxteam.debug.start').then(value => ({ value }), error => ({ error: String(error) }));
  try {
    await Promise.race([staleEntered, staleLaunching.then(() => { throw new Error('旧启动没有进入冻结声明后的配置读取'); })]);
    await bindDebugger(owner, '0.2.0', 'debug-ui-switch-while-resolving');
    await expect.poll(async () => {
      const snapshot = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: owner.gatewayId } });
      ready = snapshot.plugins.find(item => item.binding.plugin_id === 'debugging' && item.binding.workspace_id === owner.workspaceId);
      if (ready?.state === 'failed') throw new Error(`新版Debug插件启动失败: ${JSON.stringify(ready)}`);
      return [ready?.state, ready?.binding.package_version];
    }, { timeout: 45_000 }).toEqual(['ready', '0.2.0']);
    assert.notEqual(ready.instance.instance_id, originalPlugin.instance.instance_id);
    const siblingState = (await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: owner.gatewayId } }))
      .plugins.find(item => item.binding.workspace_id === otherOwner.workspaceId && item.binding.plugin_id === 'debugging');
    assert.equal(siblingState.instance.instance_id, siblingReady.instance.instance_id);
    const declaration = async target => (await api(`/api/v1/debug/node/debugger-contributions?workspace_id=${target.workspaceId}`, {}, target)).debuggers[0];
    const currentDeclaration = await declaration(owner), siblingDeclaration = await declaration(otherOwner);
    assert.equal(currentDeclaration.package_version, '0.2.0'); assert.equal(currentDeclaration.label, 'Node.js v2');
    assert.deepEqual(currentDeclaration.configuration_schema.properties.v2Only, { type: 'string' });
    assert.equal(siblingDeclaration.package_version, '0.1.0'); assert.equal(siblingDeclaration.label, 'Node.js');
    assert.equal(siblingDeclaration.configuration_schema.properties.v2Only, undefined);
    result.targetDeclarations = { current: currentDeclaration, sibling: siblingDeclaration };
    const rejected = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/debug/node/start');
    releaseStaleConfiguration();
    await expect(quick.getByText('UI shared Node', { exact: true })).toBeVisible();
    await quick.getByText('UI shared Node', { exact: true }).click();
    const response = await rejected;
    assert.equal(response.status(), 400);
    const sent = response.request().postDataJSON(), failure = await response.json();
    assert.equal(sent.debugger_registration_id, originalDeclaration.registration_id);
    assert.equal(sent.session_id, owner.sessionId); assert.equal(sent.thread_id, owner.threadId);
    assert.equal(failure.request_id, response.headers()['x-request-id']);
    assert(JSON.stringify(failure).includes('冻结的 Node Debug 插件注册已失效'));
    const launchError = page.locator('.monaco-dialog-box');
    await expect(launchError).toContainText('冻结的 Node Debug 插件注册已失效');
    await launchError.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(launchError).not.toBeVisible();
    const outcome = await staleLaunching;
    assert.equal(outcome.error, undefined, '用户关闭错误对话框后，原命令应正常结清');
    const unchanged = await state();
    assert.equal(unchanged.status, 'idle'); assert.equal(unchanged.run_id ?? null, null);
    assert.equal((await otherState()).status, 'idle');
    assert.equal(result.dap.length, 0, '被拒绝的旧声明不能创建目标或打开DAP');
    result.staleLaunch = { status: response.status(), request: sent, failure, outcome, ownerIdle: true,
      newVersion: ready.binding.package_version, oldPluginPid: originalPlugin.instance.process_id };
    result.instance = ready.instance;
  } finally {
    releaseStaleConfiguration();
    await page.unroute(stalePattern, delayStaleConfiguration);
  }
  result.phase = 'debug-start';
  let releaseConfiguration;
  const configurationBarrier = new Promise(resolve => { releaseConfiguration = resolve; });
  let enteredConfiguration;
  const configurationEntered = new Promise(resolve => { enteredConfiguration = resolve; });
  const configurationPattern = url => url.pathname === '/api/v1/debug/node/configurations'
    && url.searchParams.get('session_id') === owner.sessionId && url.searchParams.get('thread_id') === owner.threadId;
  const delayConfiguration = async route => {
    enteredConfiguration();
    await configurationBarrier;
    await route.continue();
  };
  await page.route(configurationPattern, delayConfiguration);
  const launching = command('boxteam.debug.start');
  try {
    await Promise.race([configurationEntered, launching.then(() => { throw new Error('调试启动未读取冻结 Thread 的配置'); })]);
    await otherPane.getByTestId('composer-input').click();
    releaseConfiguration();
    await expect(quick.getByText('UI shared Node', { exact: true })).toBeVisible();
    await expect(quick).toContainText('Node.js v2');
    await quick.getByText('UI shared Node', { exact: true }).click();
    await launching;
  } finally {
    releaseConfiguration();
    await page.unroute(configurationPattern, delayConfiguration);
  }
  let paused;
  await expect.poll(async () => { paused = await state(); return [paused.status, paused.call_stack[0]?.line]; }, { timeout: 20_000 }).toEqual(['paused', 2]);
  assert.equal(paused.debugger_binding.instance_id, ready.instance.instance_id);
  result.runId = paused.run_id; result.pid = paused.pid; result.initialBreakpoint = true;
  const untouched = await otherState();
  assert.equal(untouched.status, 'idle');
  assert.equal(untouched.run_id ?? null, null);
  result.frozenOrigin = { ...owner, focusMovedTo: otherOwner, otherThreadUntouched: true };
  await expect(page.locator('.debug-call-stack')).toBeVisible();
  await expect.poll(() => result.dap.some(item => item.direction === 'in' && item.value.command === 'stackTrace' && item.value.success)).toBe(true);
  const stack = result.dap.findLast(item => item.direction === 'in' && item.value.command === 'stackTrace').value.body.stackFrames;
  assert(stack[0].source.path.startsWith('boxteam://'));
  // 第二个真实 DAP 消费者保留旧引用，用后续新暂停验证代际失效。
  result.referenceProbe = await page.evaluate(async ({ owner, runId }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const url = new URL(`/api/v1/debug/node/runs/${runId}/dap`, location.href);
    url.protocol = 'ws:';
    for (const [key, value] of Object.entries({ token: credential.data.token, gateway_id: owner.gatewayId,
      workspace_id: owner.workspaceId, session_id: owner.sessionId, thread_id: owner.threadId })) url.searchParams.set(key, value);
    const socket = new WebSocket(url), pending = new Map();
    let sequence = 0;
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.type !== 'response') return;
      const entry = pending.get(message.request_seq);
      if (entry) { pending.delete(message.request_seq); clearTimeout(entry.timer); entry.resolve(message); }
    });
    globalThis.debugDapProbe = {
      socket,
      request(command, args = {}) {
        const seq = ++sequence;
        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => { pending.delete(seq); reject(new Error(`DAP probe timeout: ${command}`)); }, 10_000);
          pending.set(seq, { resolve, timer });
          socket.send(JSON.stringify({ seq, type: 'request', command, arguments: args }));
        });
      },
    };
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = () => reject(new Error('DAP probe connection failed')); });
    const initialized = await globalThis.debugDapProbe.request('initialize', { adapterID: 'boxteam-debug-probe' });
    if (!initialized.success) throw new Error(initialized.message);
    const stack = await globalThis.debugDapProbe.request('stackTrace', { threadId: 1 });
    const frameId = stack.body.stackFrames[0].id;
    const scopes = await globalThis.debugDapProbe.request('scopes', { frameId });
    const variablesReference = scopes.body.scopes.find(scope => scope.presentationHint !== 'global').variablesReference;
    const rereadStack = await globalThis.debugDapProbe.request('stackTrace', { threadId: 1 });
    if (!rereadStack.success || rereadStack.body.stackFrames[0].id !== frameId) throw new Error('同一暂停内重读调用栈必须保留已发布句柄');
    const variables = await globalThis.debugDapProbe.request('variables', { variablesReference });
    if (!variables.success) throw new Error(variables.message);
    const source = await globalThis.debugDapProbe.request('source', { sourceReference: 0 });
    if (source.success) throw new Error('未提供源码引用时不能返回空正文伪装成功');
    return { frameId, variablesReference, variables: variables.body.variables };
  }, { owner, runId: result.runId });
  // 记录真实已暂停 run 的暖请求总耗时，不把网络耗时称为插件内部耗时。
  result.warmRequests = [];
  for (let sample = 0; sample < 3; sample++) {
    const started = performance.now();
    const current = await state();
    assert.equal(current.run_id, result.runId);
    result.warmRequests.push({ operation: 'state', milliseconds: performance.now() - started });
  }
  result.warmVariables = await page.evaluate(async reference => {
    const samples = [];
    for (let sample = 0; sample < 3; sample++) {
      const started = performance.now();
      const value = await globalThis.debugDapProbe.request('variables', { variablesReference: reference });
      if (!value.success) throw new Error(value.message);
      samples.push({ operation: 'dap.variables', milliseconds: performance.now() - started });
    }
    return samples;
  }, result.referenceProbe.variablesReference);
  result.phase = 'step-and-variables';
  await command('workbench.action.debug.stepOver');
  await expect.poll(async () => { paused = await state(); return [paused.status, paused.call_stack[0]?.line]; }).toEqual(['paused', 3]);
  result.stepOver = true;
  result.staleReferences = await page.evaluate(async probe => {
    const variables = await globalThis.debugDapProbe.request('variables', { variablesReference: probe.variablesReference });
    const evaluated = await globalThis.debugDapProbe.request('evaluate', { frameId: probe.frameId, expression: 'state.counter += 100' });
    return { variables, evaluated };
  }, result.referenceProbe);
  assert.equal(result.staleReferences.variables.success, false);
  assert.equal(result.staleReferences.evaluated.success, false);
  const moduleScope = page.locator('.debug-variables .monaco-list-row').filter({
    has: page.getByText('Module', { exact: true }),
  });
  await expect(moduleScope).toBeVisible();
  if (await moduleScope.getAttribute('aria-expanded') === 'false') await moduleScope.locator('.monaco-tl-twistie').click();
  await expect(page.locator('.debug-variables')).toContainText('state');
  await expect.poll(() => result.dap.some(item => item.direction === 'in' && item.value.command === 'variables'
    && item.value.success && item.value.body.variables.some(variable => variable.name === 'state' && variable.type === 'object'))).toBe(true);
  result.phase = 'parallel-thread-run';
  await command('workbench.action.focusSecondEditorGroup');
  await expect(otherPane.getByTestId('composer-input')).toBeVisible();
  await otherPane.getByTestId('composer-input').click();
  const otherLaunching = command('boxteam.debug.start');
  await expect(quick.getByText('Other Thread Node', { exact: true })).toBeVisible();
  await quick.getByText('Other Thread Node', { exact: true }).click();
  await otherLaunching;
  let otherPaused;
  await expect.poll(async () => { otherPaused = await otherState(); return [otherPaused.status, otherPaused.call_stack[0]?.line]; },
    { timeout: 20_000 }).toEqual(['paused', 2]);
  assert.notEqual(otherPaused.run_id, result.runId);
  assert.notEqual(otherPaused.pid, result.pid);
  assert.equal(otherPaused.debugger_binding.instance_id, siblingReady.instance.instance_id);
  assert.equal((await state()).call_stack[0].line, 3);
  const debugSessionName = name => page.locator('.debug-call-stack .session .name').getByText(name, { exact: true });
  const selectedDebugSessionName = () => page.locator('.debug-toolbar .select-container .monaco-select-box')
    .evaluate(element => element.selectedOptions[0]?.textContent);
  // 通过真实调用栈选择要停止的运行，并确认 Debug Session 选择器已同步焦点。
  const otherDebugSession = debugSessionName('Other Thread Node · Debug focus destination');
  await otherDebugSession.click();
  await expect.poll(selectedDebugSessionName).toBe('Other Thread Node · Debug focus destination');
  await expect(page.locator('.debug-variables')).toContainText('state');
  await command('workbench.action.debug.stop');
  await expect.poll(async () => (await otherState()).status, { timeout: 15_000 }).toBe('exited');
  await expect(otherDebugSession).not.toBeVisible();
  assert.equal(result.dap.filter(item => item.channel === `/api/v1/debug/node/runs/${otherPaused.run_id}/dap`
    && item.value.event === 'terminated').length, 1);
  assert.equal((await state()).run_id, result.runId);
  assert.equal((await state()).status, 'paused');
  result.parallelThread = { runId: otherPaused.run_id, pid: otherPaused.pid, stopped: true, originalStillPaused: true };
  result.phase = 'agent-shared-run';
  const calls = await testControl({ action: 'plan_model', session_id: owner.sessionId, thread_id: owner.threadId, interactions: input.modelInteractions });
  result.modelCallIds = calls;
  const accepted = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`, { method: 'POST', body: {
    message: { content: '读取当前 UI 暂停运行的 state，然后将 state.counter 加十，不要重新启动。' },
    run: { mode: 'single_agent', agent_id: 'default' } } }, owner);
  await expect.poll(async () => (await api(`/api/v1/jobs/${accepted.job_id}`, {}, owner)).status, { timeout: 45_000 }).toMatch(/^(completed|succeeded)$/);
  paused = await state(); assert.equal(paused.run_id, result.runId); assert.equal(paused.status, 'paused');
  assert(paused.actions.some(action => action.actor === 'ai' && action.tool_name === 'evaluate_expression'));
  const trace = await api(`/api/v1/sessions/${owner.sessionId}/traces`, {}, owner);
  const loaded = trace.items.find(item => item.type === 'tool_call_end' && item.raw?.payload?.tool_name === 'skill_load');
  assert(loaded, 'Agent 必须先通过产品入口加载插件 Skill');
  assert.equal(loaded.raw.payload.status, 'success');
  result.skillLoad = JSON.parse(loaded.raw.payload.result);
  assert.equal(result.skillLoad.status, 'loaded', JSON.stringify(result.skillLoad));
  assert.equal(result.skillLoad.name, 'debugging');
  assert.equal(result.skillLoad.queued, true);
  assert(result.skillLoad.display_uri.startsWith(`boxteam://gateway/${owner.gatewayId}/`));
  const discovered = trace.items.find(item => item.type === 'tool_call_end' && item.raw?.payload?.tool_name === 'search_tools');
  result.discovery = JSON.parse(discovered.raw.payload.result);
  assert.equal(result.discovery.results.length, 2);
  for (const definition of result.discovery.results) {
    assert.deepEqual(definition.skill_names, ['debugging']);
    assert.equal(definition.skill_associations.length, 1);
    assert.equal(definition.skill_associations[0].status, 'available');
    assert(definition.skill_associations[0].display_uri.startsWith(`boxteam://gateway/${owner.gatewayId}/`));
  }
  const toolNames = ['get_variables_values', 'evaluate_expression'];
  result.traces = trace.items.filter(item => ['tool_call_start', 'tool_call_end'].includes(item.type)
    && toolNames.includes(item.raw?.payload?.tool_name));
  for (const [index, name] of toolNames.entries()) {
    const entries = result.traces.filter(item => item.raw.payload.tool_name === name);
    assert.deepEqual(entries.map(item => item.type), ['tool_call_start', 'tool_call_end']);
    assert.equal(entries[0].part_id, entries[1].part_id);
    assert(entries[0].part_id);
    for (const entry of entries) assert.equal(entry.raw.payload.execution_id, entry.part_id);
    const toolCallId = `debug-ui-agent-${index + 2}`;
    assert.equal(entries[1].raw.payload.tool_call_id, toolCallId);
    assert(paused.actions.some(action => action.actor === 'ai' && action.tool_name === name
      && action.tool_call_id === toolCallId));
    const toolResult = JSON.parse(entries[1].raw.payload.result);
    assert.equal(toolResult.ok, true, JSON.stringify(toolResult));
    assert.equal(toolResult.state.run_id, result.runId);
    if (name === 'get_variables_values') assert(toolResult.variables.some(item => item.name === 'state' && item.type === 'object'));
    else assert.equal(toolResult.state.last_evaluation.value, '13');
  }
  result.agentSharedRun = true;
  result.phase = 'lost-evaluate-response';
  // REPL 命令使用当前 Debug 会话；停止另一个 Thread 后先显式选回暂停中的原 run。
  const originalDebugSession = debugSessionName('UI shared Node · Debug UI shared run');
  await originalDebugSession.click();
  await expect.poll(selectedDebugSessionName).toBe('UI shared Node · Debug UI shared run');
  await expect.poll(async () => (await state()).status).toBe('paused');
  result.replSelection = { sessionId: owner.sessionId, threadId: owner.threadId, runId: result.runId };
  loseEvaluation = true;
  await command('workbench.debug.action.focusRepl');
  const consoleInput = page.locator('.repl-input-wrapper .monaco-editor').filter({ visible: true });
  await consoleInput.click();
  await page.keyboard.insertText('state.counter += 1');
  await page.keyboard.press('Enter');
  await expect.poll(() => result.dap.filter(item => item.direction === 'out' && item.value.command === 'evaluate'
    && item.value.arguments?.expression === 'state.counter += 1').length).toBe(1);
  const evaluateRequests = result.dap.filter(item => item.direction === 'out' && item.value.command === 'evaluate'
    && item.value.arguments?.expression === 'state.counter += 1');
  assert.equal(evaluateRequests.length, 1);
  assert.equal(evaluateRequests[0].channel, `/api/v1/debug/node/runs/${result.runId}/dap`);
  await expect.poll(() => result.responseLoss?.response.success).toBe(true);
  assert.equal(result.responseLoss.response.body.result, '14');
  // UI 连接失效不停止目标；从原业务入口读取事实，副作用只执行一次。
  const afterLoss = await state();
  assert.equal(afterLoss.run_id, result.runId);
  assert.equal(afterLoss.status, 'paused');
  assert.equal(afterLoss.last_evaluation.value, '14');
  assert.equal(afterLoss.evaluations.filter(item => item.expression === 'state.counter += 1').length, 1);
  assert.equal(lostEvaluationRequests, 1);
  result.phase = 'continue-and-stop';
  const action = action => api('/api/v1/debug/node/action', { method: 'POST', body: {
    session_id: owner.sessionId, thread_id: owner.threadId, action,
  } }, owner);
  await action('continue');
  await expect.poll(async () => (await state()).output.join('\n')).toContain('debug-result 17');
  await action('stop');
  await expect.poll(async () => (await state()).status, { timeout: 15_000 }).toBe('exited');
  assert.equal(lostEvaluationRequests, 1, '连接断开及后续操作不得重放有副作用的求值');
  result.finalState = await state(); result.actualExit = true;
  result.phase = 'unbind-and-uninstall';
  await bindDebugger(owner, null, 'debug-ui-unbind-after-actual-exit');
  await expect.poll(async () => {
    const snapshot = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: owner.gatewayId } });
    return snapshot.plugins.some(item => item.binding.package_id === 'boxteam.developer-tools'
      && item.binding.plugin_id === 'debugging' && item.binding.workspace_id === owner.workspaceId);
  }, { timeout: 15_000 }).toBe(false);
  result.phase = 'sibling-after-origin-unbind';
  const remaining = (await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: owner.gatewayId } }))
    .plugins.find(item => item.binding.workspace_id === otherOwner.workspaceId && item.binding.plugin_id === 'debugging');
  assert.equal(remaining.state, 'ready');
  assert.equal(remaining.instance.instance_id, siblingReady.instance.instance_id);
  await otherPane.getByTestId('composer-input').click();
  const siblingLaunching = command('boxteam.debug.start');
  await expect(quick.getByText('Other Thread Node', { exact: true })).toBeVisible();
  await expect(quick).toContainText('Node.js');
  await expect(quick).not.toContainText('Node.js v2');
  await quick.getByText('Other Thread Node', { exact: true }).click();
  await siblingLaunching;
  let siblingPaused;
  await expect.poll(async () => { siblingPaused = await otherState(); return [siblingPaused.status, siblingPaused.call_stack[0]?.line]; }).toEqual(['paused', 2]);
  assert.equal(siblingPaused.debugger_binding.instance_id, siblingReady.instance.instance_id);
  assert.notEqual(siblingPaused.run_id, otherPaused.run_id);
  // 仅剩一个真实会话时，原版调用栈会省略 session 行；由新 run 的变量回执确认已聚焦。
  await expect(page.locator('.debug-variables')).toContainText('state');
  await expect.poll(() => result.dap.some(item => item.channel === `/api/v1/debug/node/runs/${siblingPaused.run_id}/dap`
    && item.direction === 'in' && item.value.command === 'variables' && item.value.success)).toBe(true);
  await command('workbench.action.debug.stepOver');
  await expect.poll(async () => (await otherState()).call_stack[0]?.line).toBe(3);
  assert.equal(result.dap.findLast(item => item.direction === 'out' && item.value.command === 'next').channel,
    `/api/v1/debug/node/runs/${siblingPaused.run_id}/dap`);
  await command('workbench.action.debug.stop');
  await expect.poll(async () => (await otherState()).status).toBe('exited');
  result.siblingAfterUnbind = { pid: siblingPaused.pid, pluginPid: siblingReady.instance.process_id, originalInstanceKept: true };
  await bindDebugger(otherOwner, null, 'debug-ui-unbind-sibling');
  result.uninstalled = [];
  for (const version of ['0.1.0', '0.2.0']) {
    const removed = await api('/api/gateway/service-plugins/uninstall', { method: 'POST', body: {
      gateway_id: owner.gatewayId, package_id: 'boxteam.developer-tools', package_version: version,
    } });
    assert.deepEqual(removed, { package_id: 'boxteam.developer-tools', package_version: version, uninstalled: true });
    result.uninstalled.push(removed);
  }
  assert.equal(result.pageErrors.length, 0, result.pageErrors.join('\n'));
  result.phase = 'complete';
} catch (error) {
  result.error = String(error.stack ?? error);
  errors.push(error);
} finally {
  // 诊断失败不能跳过实际 Chromium 清理，原断言与清理错误一并保留。
  for (const cleanup of [
    async () => { if (page) await page.evaluate(() => globalThis.debugDapProbe?.socket.close()); },
    async () => { if (page) await page.screenshot({ path: join(input.artifacts, 'debug-ui.png'), fullPage: true }); },
    () => writeFile(join(input.artifacts, 'debug-ui.json'), JSON.stringify(result, null, 2)),
    async () => { if (browser) await browser.close(); },
  ]) {
    try { await cleanup(); } catch (error) { errors.push(error); }
  }
}
if (errors.length) throw new AggregateError(errors, 'Workbench Debug 完整流程或资源清理失败');
