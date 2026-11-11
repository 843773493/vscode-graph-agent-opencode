import assert from 'node:assert/strict';
import { once } from 'node:events';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// 驱动也只用发行物的 Node、Playwright 和 Chromium，不借开发依赖。
const input = JSON.parse(process.env.BOXTEAM_DISTRIBUTION_WORKBENCH);
const localOrigins = new Set([new URL(input.baseUrl).origin]);
const { chromium, _electron, expect } = await import(pathToFileURL(path.join(input.application, 'node_modules/playwright/test.mjs')).href);
const result = { phase: 'initialize', pageErrors: [], externalRequests: [], exitedPids: [], calls: [],
  native: { launches: [], exits: [], storageErrors: [] } };
let browser, nativeApp, page;
const observePage = next => {
  page = next;
  page.setDefaultTimeout(20_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && /IndexedDB|database.*closed/i.test(message.text())) {
      result.native.storageErrors.push(message.text());
    }
  });
  page.on('dialog', dialog => dialog.type() === 'beforeunload' ? dialog.accept() : dialog.dismiss());
  page.on('request', request => {
    const url = new URL(request.url());
    const document = new URL(next.url());
    if (['http:', 'https:'].includes(url.protocol) && document.protocol === 'http:'
      && url.origin !== document.origin && !localOrigins.has(url.origin)) {
      result.externalRequests.push(request.url());
    }
  });
};
const openNative = async () => {
  nativeApp = await _electron.launch({ executablePath: input.electron,
    args: ['--no-sandbox', `--gateway=${input.baseUrl}`], cwd: input.cwd,
    env: { ...process.env, BOXTEAM_HOME: input.profile, PATH: input.emptyPath }, timeout: 60_000 });
  nativeApp.process().stdout.on('data', chunk => process.stdout.write(chunk));
  nativeApp.process().stderr.on('data', chunk => process.stderr.write(chunk));
  const location = await nativeApp.evaluate(({ app }) => ({ packaged: app.isPackaged,
    appPath: app.getAppPath(), cwd: process.cwd(), path: process.env.PATH, userData: app.getPath('userData') }));
  assert.equal(location.packaged, true);
  assert.ok(location.appPath.endsWith('/resources/app.asar'));
  assert.equal(location.cwd, input.cwd); assert.equal(location.path, input.emptyPath);
  assert.equal(location.userData, path.join(input.profile, 'clients/electron'));
  result.native.launches.push(location);
  observePage(await nativeApp.firstWindow());
  await page.waitForURL(url => url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.pathname === '/',
    { waitUntil: 'domcontentloaded' });
  location.rendererUrl = page.url();
  localOrigins.add(new URL(location.rendererUrl).origin);
  assert.deepEqual(await page.evaluate(() => [typeof globalThis.require, typeof globalThis.process]), ['undefined', 'undefined']);
};
const closeNative = async () => {
  if (!nativeApp) return;
  const exited = once(nativeApp.process(), 'exit', { signal: AbortSignal.timeout(30_000) });
  await nativeApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  const [code, signal] = await exited;
  result.native.exits.push({ code, signal });
  nativeApp = undefined;
  assert.equal(code, 0); assert.equal(signal, null);
  assert.deepEqual(result.native.storageErrors, []);
};
try {
  if (input.electron) await openNative();
  else {
    browser = await chromium.launch({ executablePath: input.chromium, headless: true });
    observePage(await browser.newPage({ viewport: { width: 1500, height: 950 } }));
    await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  }
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const workbenchUrl = page.url();
  const api = (url, body, raw = false) => page.evaluate(async ({ url, body, raw }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(url, { method: body ? 'POST' : 'GET',
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(`${url}: ${response.status} ${JSON.stringify(payload)}`);
    }
    return raw ? payload : payload.data;
  }, { url, body, raw });
  result.initialization = [];
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    result.initialization.push({ url, data: await api(url) });
  }
  const active = result.initialization[1].data.active_workspace;
  assert.equal(active.gateway_id, result.initialization[0].data.gateway_id);
  assert.equal(active.workspace_id, result.initialization[2].data.workspace_id);
  result.phase = 'browser-attach';
  const attachment = input.browserResource;
  assert.equal(attachment.gatewayId, active.gateway_id);
  assert.equal(attachment.workspaceId, active.workspace_id);
  const browserSockets = [];
  page.on('websocket', socket => {
    if (!new URL(socket.url()).pathname.includes('/services/socket/browser')) return;
    const observation = { path: new URL(socket.url()).pathname, binaryFrames: 0, input: [], replies: [] };
    browserSockets.push(observation);
    result.browserTransports = browserSockets;
    socket.on('framesent', frame => {
      if (typeof frame.payload !== 'string') return;
      const message = JSON.parse(frame.payload);
      if (message.type === 'pointer') observation.input.push(message);
    });
    socket.on('framereceived', frame => {
      if (Buffer.isBuffer(frame.payload)) observation.binaryFrames++;
      else {
        const message = JSON.parse(frame.payload);
        if (['error', 'operationAck'].includes(message.type)) observation.replies.push(message);
      }
    });
  });
  const attachUrl = new URL('/api/gateway/attach/browser/index.html', input.baseUrl);
  attachUrl.search = new URLSearchParams({ browserId: attachment.resourceId, workspaceId: attachment.workspaceId,
    target_gateway_id: attachment.gatewayId }).toString();
  await page.goto(attachUrl.href, { waitUntil: 'domcontentloaded' });
  const attachToggle = page.locator('#attach-toggle');
  await expect(attachToggle).toHaveAttribute('aria-label', '断开浏览器');
  await page.locator('#address-input').fill('data:text/html,<title>distribution-browser-page</title>'
    + '<style>html,body{margin:0;width:100%;height:100%;background:rgb(0,128,0)}</style>'
    + '<body onclick="document.body.style.background=\'rgb(0,0,128)\'">release</body>');
  await page.locator('#address-input').press('Enter');
  const pixel = () => page.locator('#screen-canvas').evaluate(canvas =>
    Array.from(canvas.getContext('2d').getImageData(100, 100, 1, 1).data).slice(0, 3));
  const greenFrame = async () => { const [red, green, blue] = await pixel(); return red < 20 && green > 110 && green < 150 && blue < 20; };
  await expect.poll(greenFrame, { timeout: 30_000 }).toBe(true);
  await page.locator('#screen-canvas').click({ position: { x: 100, y: 100 } });
  const blueFrame = async () => { const [red, green, blue] = await pixel(); return red < 20 && green < 20 && blue > 110 && blue < 150; };
  await expect.poll(blueFrame).toBe(true);
  await attachToggle.click();
  await expect(attachToggle).toHaveAttribute('aria-label', '连接浏览器');
  await page.locator('#screen-canvas').evaluate(canvas => canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height));
  await attachToggle.click();
  await expect(attachToggle).toHaveAttribute('aria-label', '断开浏览器');
  await expect.poll(() => browserSockets[1]?.binaryFrames ?? 0).toBeGreaterThan(0);
  await expect.poll(blueFrame).toBe(true);
  assert.equal(browserSockets.length, 2);
  assert.ok(browserSockets.every(({ path }) => path.includes(`/instances/${attachment.instanceId}/`)));
  result.browserAttachment = { ...attachment, pixel: await pixel(), reconnected: true, connections: browserSockets.length };
  await page.goto(workbenchUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const target = { gateway_id: active.gateway_id };
  const slots = async () => Object.fromEntries((await api('/api/gateway/service-plugins/status', target)).plugins
    .filter(item => item.binding.package_id === 'distribution.probe').map(item => [item.binding.plugin_id, item]));
  const call = async slot => {
    assert.equal(slot.state, 'ready');
    const value = await api(`/api/gateway/service-plugins/distribution.probe/${slot.binding.plugin_id}/instances/`
      + `${slot.instance.instance_id}/services/echo/value?package_version=${slot.binding.package_version}`, undefined, true);
    assert.equal(value.instance_id, slot.instance.instance_id);
    assert.equal(value.pid, slot.instance.process_id);
    assert.equal(value.version, slot.binding.package_version);
    result.calls.push(value);
  };
  const openManagement = async () => {
    await page.waitForFunction(() => typeof globalThis.workbench?.commands?.executeCommand === 'function');
    await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
    await expect(page.locator('.service-plugin-packages-view')).toBeVisible();
  };
  await openManagement();
  let view = page.locator('.service-plugin-packages-view');
  let quick = page.locator('.quick-input-widget');
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click();
  };
  const row = version => view.locator('.service-plugin-package').filter({
    has: page.getByRole('heading', { name: `发行闭环探针 · distribution.probe@${version}`, exact: true }),
  });
  const install = async (version, file) => {
    await view.getByRole('button', { name: '安装插件包', exact: true }).click();
    await pick('ZIP 文件');
    const field = quick.locator('input').first();
    await field.fill(file); await field.press('Enter');
    await expect(row(version)).toBeVisible({ timeout: 30_000 });
  };
  const open = async (version, plugin) => {
    await row(version).getByRole('button', { name: `${plugin} · ${plugin}`, exact: true }).click();
    await expect(view.locator('.service-plugin-details')).toBeVisible();
  };
  const saveBinding = async (action, expected) => {
    await view.getByRole('button', { name: action, exact: true }).click();
    await pick('gateway_mutable_override');
    await pick('确认保存 Gateway 插件绑定');
    await expect(quick).not.toBeVisible();
    await expect(view.getByLabel('插件配置与运行版本')).toContainText(expected);
  };
  result.phase = 'install-and-bind';
  await install('1.0.0', input.packages[0]);
  assert.deepEqual(await slots(), {});
  for (const plugin of ['python', 'node']) {
    await open('1.0.0', plugin); await saveBinding('启用此范围', '配置：启用 @1.0.0');
    await expect.poll(async () => (await slots())[plugin]?.state, { timeout: 20_000 }).toBe('ready');
    await call((await slots())[plugin]);
  }
  const original = await slots();
  assert.notEqual(original.python.instance.process_id, original.node.instance.process_id);
  result.phase = 'session-and-settings';
  const session = await api('/api/v1/sessions', { title: '发行客户端恢复', agent_id: 'default' });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`);
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  await command('_workbench.open', `boxteam-thread://${active.gateway_id}/${active.workspace_id}/${session.session_id}/${main.thread_id}`,
    [undefined, { pinned: true }]);
  const thread = () => page.locator(`section.boxteam-thread[data-workspace-id="${active.workspace_id}"]`
    + `[data-session-id="${session.session_id}"][data-thread-id="${main.thread_id}"]`);
  const prompt = '验证发行客户端会话与设置恢复。';
  await thread().getByTestId('composer-input').fill(prompt);
  const acceptedResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/v1/sessions/${session.session_id}/threads/${main.thread_id}/messages`);
  await thread().getByTestId('composer-send').click();
  const acceptedReply = await acceptedResponse;
  assert.equal(acceptedReply.status(), 200);
  const acceptance = await acceptedReply.json();
  assert.equal(acceptance.request_id, acceptedReply.headers()['x-request-id']);
  const accepted = acceptance.data;
  assert.equal(accepted.thread_id, main.thread_id);
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`);
    assert.ok(!['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status), JSON.stringify(job));
    return job.status;
  }, { timeout: 30_000 }).toBe('completed');
  await expect(thread()).toContainText('发行会话恢复成功。');
  const history = () => api(`/api/v1/sessions/${session.session_id}/threads/${main.thread_id}/history`,
    { turn_ids: [accepted.turn_id], include: ['user', 'assistant_text', 'final_response'] });
  const beforeHistory = await history();
  assert.equal(beforeHistory.items.length, 1);
  assert.equal(beforeHistory.items[0].turn_id, accepted.turn_id);
  assert.ok(beforeHistory.items[0].source_message_ids.includes(accepted.message_id));
  await command('workbench.action.openWorkspaceSettingsFile');
  const settings = page.getByRole('dialog', { name: /settings\.json$/ });
  const editor = settings.locator('.monaco-editor [role="textbox"]');
  await editor.focus(); await editor.press('ControlOrMeta+A');
  await page.keyboard.insertText('{"editor.fontSize":19}\n');
  await expect(settings.locator('.view-lines')).toContainText('"editor.fontSize":19');
  await command('workbench.action.files.save');
  await command('workbench.action.closeActiveEditor');
  await expect(thread().getByTestId('composer-input')).toBeVisible();
  if (input.electron) { await closeNative(); await openNative(); }
  else await page.reload({ waitUntil: 'domcontentloaded' });
  assert.equal(page.url(), workbenchUrl);
  await expect(thread().getByTestId('composer-input')).toBeVisible();
  await expect(thread()).toContainText(prompt);
  await expect(thread()).toContainText('发行会话恢复成功。');
  assert.deepEqual(await history(), beforeHistory);
  await command('workbench.action.openWorkspaceSettingsFile');
  const restoredSettings = page.getByRole('dialog', { name: /settings\.json$/ });
  await expect(restoredSettings.locator('.view-lines')).toContainText('"editor.fontSize":19');
  await expect.poll(() => restoredSettings.locator('.view-line').first()
    .evaluate(element => getComputedStyle(element).fontSize)).toBe('19px');
  await command('workbench.action.closeActiveEditor');
  result.sessionRecovery = { sessionId: session.session_id, threadId: main.thread_id,
    turnId: accepted.turn_id, messageId: accepted.message_id, requestId: acceptance.request_id,
    sourceMessageIds: beforeHistory.items[0].source_message_ids, restoredFontSize: 19 };
  await openManagement();
  view = page.locator('.service-plugin-packages-view');
  quick = page.locator('.quick-input-widget');
  assert.deepEqual(await slots(), original);
  result.phase = 'update-and-switch';
  await install('2.0.0', input.packages[1]);
  assert.deepEqual(await slots(), original);
  for (const plugin of ['python', 'node']) {
    const before = await slots();
    await open('2.0.0', plugin); await saveBinding('切换到此版本（保留开关）', '配置：启用 @2.0.0');
    const current = await slots();
    assert.equal(current[plugin].binding.package_version, '2.0.0');
    assert.notEqual(current[plugin].instance.instance_id, before[plugin].instance.instance_id);
    const sibling = plugin === 'python' ? 'node' : 'python';
    assert.deepEqual(current[sibling].instance, before[sibling].instance);
    result.exitedPids.push(before[plugin].instance.process_id);
    await call(current[plugin]);
  }
  result.phase = 'restart';
  const beforeRestart = await slots();
  await open('2.0.0', 'python');
  const restarted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/restart');
  await view.getByRole('button', { name: '重启实例', exact: true }).click();
  assert.equal((await restarted).status(), 200);
  const current = await slots();
  assert.notEqual(current.python.instance.instance_id, beforeRestart.python.instance.instance_id);
  assert.deepEqual(current.node.instance, beforeRestart.node.instance);
  result.exitedPids.push(beforeRestart.python.instance.process_id);
  await call(current.python);
  result.phase = 'disable-and-uninstall';
  for (const plugin of ['python', 'node']) {
    await open('2.0.0', plugin); await saveBinding('停用此范围', '配置：停用 @2.0.0');
    assert.equal((await slots())[plugin].state, 'stopped');
    result.exitedPids.push(current[plugin].instance.process_id);
  }
  const blocked = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/uninstall');
  await row('2.0.0').getByRole('button', { name: '卸载整个包', exact: true }).click(); await pick('卸载整包');
  assert.equal((await blocked).status(), 409);
  await expect(page.locator('.notifications-toasts')).toContainText('configuration:');
  for (const plugin of ['python', 'node']) {
    await open('2.0.0', plugin); await saveBinding('解除此范围绑定', '配置：未绑定');
  }
  assert.deepEqual(await slots(), {});
  for (const version of ['1.0.0', '2.0.0']) {
    await row(version).getByRole('button', { name: '卸载整个包', exact: true }).click(); await pick('卸载整包');
    await expect(row(version)).toHaveCount(0);
  }
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.externalRequests, []);
  assert.deepEqual(result.native.storageErrors, []);
  result.phase = 'complete';
  await page.screenshot({ path: input.screenshot, fullPage: true });
} catch (error) {
  result.error = String(error?.stack ?? error);
  if (page && !page.isClosed()) await page.screenshot({ path: input.screenshot, fullPage: true });
  throw error;
} finally {
  try { await closeNative(); await browser?.close(); }
  finally { await writeFile(input.resultPath, JSON.stringify(result, null, 2) + '\n'); }
}
