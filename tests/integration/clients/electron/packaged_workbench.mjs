import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createWriteStream } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { once } from 'node:events';
import { finished } from 'node:stream/promises';
import { createHash } from 'node:crypto';
import httpProxy from 'http-proxy';
import { _electron, expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { testControl } from '../../../support/e2e_control/browser_client.mjs';
import { observeImageCache } from './image_cache.mjs';

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}
const executable = required('BOXTEAM_ELECTRON_EXECUTABLE');
const gateway = required('BOXTEAM_ELECTRON_GATEWAY');
const routeId = required('BOXTEAM_ELECTRON_ROUTE_ID');
const runtime = required('BOXTEAM_ELECTRON_RUNTIME');
const artifacts = required('BOXTEAM_ELECTRON_ARTIFACTS');
const scope = TestRunContext.fromTestFile('tests/integration/clients/electron/test_packaged_workbench.py');
const cwd = join(runtime, 'launch-directory');
const emptyPath = join(runtime, 'empty-path');
const profile = join(runtime, 'profile-a');
const prompt = 'ELECTRON_PACKAGED_USER_MESSAGE';
const answer = 'ELECTRON_PACKAGED_AGENT_RESPONSE';
const draft = 'ELECTRON_PACKAGED_UNSENT_DRAFT';
const pageErrors = [], consoleErrors = [], proxyErrors = [], heldRequests = [], launches = [], receipts = [];
const storageErrors = () => consoleErrors.filter(entry => /IndexedDB|database.*closed/i.test(entry.text));
const checks = {};
const imageReads = [];
let owner, app, page, delay = false, release = () => {}, failure;
let gate = Promise.resolve();
let proxyOrigin;
await mkdir(cwd, { recursive: true });
await mkdir(emptyPath, { recursive: true });

// 延迟点位于原生应用启动之前，避免 firstWindow 返回时初始化请求已完成。
const proxy = httpProxy.createProxyServer({ target: gateway, ws: true, xfwd: true });
scope.addCleanup('Electron 测试转发器', () => proxy.close());
proxy.on('proxyReq', (outgoing, _request, response) => {
  // 关闭原生窗口后也取消上游 SSE，避免只关闭客户端而留下驱动保活连接。
  response.once('close', () => outgoing.destroy());
});
proxy.on('error', (error, _request, response) => {
  proxyErrors.push(String(error));
  if (response && 'writeHead' in response && !response.headersSent) response.writeHead(502);
  response?.end(String(error));
});
const sockets = new Set();
const server = createServer((request, response) => {
  const url = new URL(request.url, gateway);
  if (delay && request.method === 'GET' && owner
    && url.pathname === `/api/v1/sessions/${owner.session_id}/bootstrap`
    && url.searchParams.get('thread_id') === owner.thread_id
    && request.headers['x-boxteam-workspace-id'] === routeId) {
    heldRequests.push({ path: request.url, routeId: request.headers['x-boxteam-workspace-id'] });
    void gate.then(() => proxy.web(request, response));
  } else proxy.web(request, response);
});
server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
server.on('upgrade', (request, socket, head) => proxy.ws(request, socket, head));
scope.addCleanup('Electron 延迟代理监听', async () => {
  release();
  server.closeAllConnections();
  for (const socket of sockets) socket.destroy();
  if (server.listening) await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
});

function holdBootstrap() {
  delay = true;
  gate = new Promise(resolve => { release = resolve; });
}
function threadSurface(target) {
  return target.locator(`section.boxteam-thread[data-workspace-id="${owner.workspace_id}"][data-session-id="${owner.session_id}"][data-thread-id="${owner.thread_id}"]`);
}
async function payload(response) {
  assert.equal(response.status(), 200, await response.text());
  const value = await response.json();
  assert.ok(value.request_id);
  assert.equal(response.headers()['x-request-id'], value.request_id);
  receipts.push({ path: new URL(response.url()).pathname, requestId: value.request_id });
  return value.data;
}
async function api(target, path) {
  return target.evaluate(async ({ path, routeId }) => {
    const credential = await fetch('/api/gateway/auth/local-credential');
    if (!credential.ok) throw new Error(`本地凭据 HTTP ${credential.status}`);
    const token = (await credential.json()).data.token;
    const response = await fetch(path, { headers: { 'X-Local-Token': token, 'X-BoxTeam-Workspace-Id': routeId } });
    const value = await response.json();
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${JSON.stringify(value)}`);
    if (!value.request_id || response.headers.get('x-request-id') !== value.request_id) throw new Error(`request_id 不一致：${path}`);
    return value;
  }, { path, routeId });
}
async function launch(home) {
  const args = ['--no-sandbox', `--gateway=${proxyOrigin}`];
  const instance = await _electron.launch({ executablePath: executable, args, cwd,
    env: { ...process.env, BOXTEAM_HOME: home, PATH: emptyPath }, timeout: 60_000 });
  const nativeProcess = instance.process();
  const logs = [];
  scope.addCleanup(`原生应用 ${launches.length + 1}`, async () => {
    const errors = [];
    try { if (nativeProcess.exitCode === null && nativeProcess.signalCode === null) await instance.close(); }
    catch (error) { errors.push(error); }
    for (const { channel, log } of logs) {
      nativeProcess[channel].unpipe(log);
      if (!log.writableEnded) log.end();
      try { await finished(log); } catch (error) { errors.push(error); }
    }
    if (errors.length) throw new AggregateError(errors, '释放原生应用及日志失败');
  });
  const logNumber = launches.length + 1;
  for (const channel of ['stdout', 'stderr']) {
    const log = createWriteStream(join(artifacts, `electron-${logNumber}.${channel}.log`), { flags: 'wx' });
    nativeProcess[channel].pipe(log);
    logs.push({ channel, log });
  }
  const location = await instance.evaluate(({ app }) => ({ appPath: app.getAppPath(), packaged: app.isPackaged,
    userData: app.getPath('userData'), cwd: process.cwd(), path: process.env.PATH }));
  assert.equal(location.packaged, true);
  assert.ok(location.appPath.endsWith('/resources/app.asar'));
  assert.equal(location.userData, join(home, 'clients/electron'));
  assert.equal(location.cwd, cwd);
  assert.equal(location.path, emptyPath);
  const launchRecord = { args, location };
  launches.push(launchRecord);
  app = instance;
  page = await instance.firstWindow();
  page.setDefaultTimeout(30_000);
  page.on('pageerror', error => pageErrors.push(String(error)));
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push({ launch: logNumber, text: message.text(), location: message.location() });
  });
  // firstWindow 只交付窗口，先等待真实根文档加载，再开始原有控件可见性断言。
  await page.waitForURL(url => url.protocol === 'http:' && url.hostname === '127.0.0.1'
    && url.pathname === '/' && url.search === '', { waitUntil: 'domcontentloaded' });
  launchRecord.url = page.url();
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  return page;
}
async function closeNative() {
  const processHandle = app.process();
  const exited = once(processHandle, 'exit');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  const [code, signal] = await exited;
  assert.equal(code, 0);
  assert.equal(signal, null);
  app = undefined;
}

try {
  server.listen(Number(required('BOXTEAM_ELECTRON_PROXY_PORT')), '127.0.0.1');
  await once(server, 'listening');
  proxyOrigin = `http://127.0.0.1:${server.address().port}`;
  await launch(profile);
  let probeImage = await observeImageCache(page, imageReads);
  checks.packagedAsar = true;
  const origin = new URL(page.url()).origin;
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    const value = await api(page, path);
    receipts.push({ path, requestId: value.request_id });
    if (path.endsWith('/workspaces')) assert.equal(value.data.active_workspace_id, routeId);
  }
  assert.deepEqual(await page.evaluate(() => ({ require: typeof globalThis.require,
    process: typeof globalThis.process, electron: typeof globalThis.electron })),
  { require: 'undefined', process: 'undefined', electron: 'undefined' });
  const preferences = await app.evaluate(({ BrowserWindow }) => {
    const value = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences();
    return { nodeIntegration: value.nodeIntegration, contextIsolation: value.contextIsolation,
      sandbox: value.sandbox, webviewTag: value.webviewTag };
  });
  assert.deepEqual(preferences, { nodeIntegration: false, contextIsolation: true, sandbox: true, webviewTag: false });
  checks.noRendererNode = true;

  const created = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/sessions');
  await page.getByTitle('新建会话', { exact: true }).click();
  const createdResponse = await created;
  assert.equal(createdResponse.request().headers()['x-boxteam-workspace-id'], routeId);
  owner = await payload(createdResponse);
  assert.ok(owner.session_id && owner.thread_id && owner.workspace_id);
  let thread = threadSurface(page);
  // 与正式 Web Composer 初始化同为 30 秒；新 profile 还需完成模型目录读取。
  await expect(thread.getByTestId('composer-input')).toBeVisible({ timeout: 30_000 });
  const modelCallIds = await testControl({ action: 'plan_model', session_id: owner.session_id,
    thread_id: owner.thread_id, interactions: [JSON.parse(required('BOXTEAM_ELECTRON_INTERACTION'))] });
  assert.equal(modelCallIds.length, 1);
  await thread.getByTestId('composer-input').fill(prompt);
  const messagePath = `/api/v1/sessions/${owner.session_id}/threads/${owner.thread_id}/messages`;
  const sent = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === messagePath);
  await thread.getByTestId('composer-send').click();
  const sentResponse = await sent;
  assert.equal(sentResponse.request().headers()['x-boxteam-workspace-id'], routeId);
  const accepted = await payload(sentResponse);
  assert.equal(accepted.thread_id, owner.thread_id);
  await expect.poll(async () => {
    const job = (await api(page, `/api/v1/jobs/${accepted.job_id}`)).data;
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 60_000 }).toBe('completed');
  await expect(thread.locator('.chat-stream-shell')).toContainText(answer, { timeout: 60_000 });

  const listing = (await api(page, '/api/v1/workspace/files')).data;
  const imageOwner = { gatewayId: (await api(page, '/api/gateway/health')).data.gateway_id,
    workspaceId: owner.workspace_id, rootUri: listing.path };
  const listed = listing.items.find(item => item.name === 'README.md');
  assert.ok(listed, '测试工作区必须提供 README.md');
  const fileUri = listed.path;
  assert.ok(fileUri.startsWith(`boxteam://workspace/${owner.workspace_id}/`));
  const fileEndpoint = `/api/v1/workspace/files/content?scope=workspace&path=${encodeURIComponent(fileUri)}`;
  const original = (await api(page, fileEndpoint)).data;
  assert.equal(original.path, fileUri);
  const toggle = thread.getByRole('button', { name: '切换会话资源', exact: true });
  await toggle.click();
  await thread.getByRole('tab', { name: '文件', exact: true }).click();
  const root = thread.getByTitle(listing.path, { exact: true }).and(thread.locator('button.root'));
  await expect(root).toBeVisible();
  if (await root.getAttribute('aria-expanded') !== 'true') await root.click();
  const row = thread.getByTitle(fileUri, { exact: true }).and(thread.locator('button.files-tree-item'));
  await expect(row).toHaveCount(1);
  await row.click();
  await expect(thread.getByRole('region', { name: '文件代码预览区', exact: true })).toContainText('统一测试工作区');
  await expect(page.locator('.tabs-container .tab[data-resource-name="README.md"]')).toHaveCount(0);
  await row.click({ button: 'right' });
  await thread.getByRole('menuitem', { name: 'VS Code', exact: true }).click();
  await page.getByRole('menuitem', { name: '在编辑器中打开', exact: true }).click({ delay: 160 });
  const editor = page.locator('.monaco-editor:visible').getByRole('textbox');
  const content = `${original.content}\nELECTRON_PACKAGED_VIRTUAL_FILE_SAVE\n`;
  await expect(editor).toBeVisible();
  await editor.press('ControlOrMeta+A');
  await page.keyboard.insertText(content);
  const saving = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === '/api/v1/workspace/files/content');
  await editor.press('ControlOrMeta+S');
  const savedResponse = await saving;
  assert.equal(savedResponse.request().postDataJSON().expected_revision, original.revision);
  const saved = await payload(savedResponse);
  assert.equal(saved.content, content);
  assert.notEqual(saved.revision, original.revision);
  assert.equal((await api(page, fileEndpoint)).data.content, content);
  checks.fileSaved = true;
  const file = { uri: fileUri, revision: saved.revision,
    sha256: createHash('sha256').update(content).digest('hex') };
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.closeActiveEditor'));
  thread = threadSurface(page);
  await expect(thread.getByTestId('composer-input')).toBeVisible();
  const coldImage = await probeImage(imageOwner, 'cold', 200);
  const warmImage = await probeImage(imageOwner, 'warm', 304);
  assert.equal(warmImage.response.etag, coldImage.response.etag);
  checks.imageRevalidated = true;
  await thread.getByTestId('composer-input').fill(draft);
  await page.screenshot({ path: join(artifacts, 'native-synced.png') });
  await closeNative();
  checks.nativeClose = true;
  const state = JSON.parse(await readFile(join(profile, 'clients/electron/renderer-origin.json'), 'utf8'));
  assert.equal(origin, `http://127.0.0.1:${state.port}`);

  holdBootstrap();
  await launch(profile);
  probeImage = await observeImageCache(page, imageReads);
  assert.equal(new URL(page.url()).origin, origin);
  checks.stableOrigin = true;
  thread = threadSurface(page);
  const cached = thread.locator('.native-thread-cached-history');
  await expect(cached).toBeVisible();
  await expect(cached).toContainText(answer);
  await expect.poll(() => heldRequests.length).toBeGreaterThan(0);
  await expect(cached.locator('fieldset')).toHaveJSProperty('disabled', true);
  const controls = await cached.locator('button, input, textarea, select').evaluateAll(nodes => nodes.map(node => node.matches(':disabled')));
  assert.ok(controls.length > 0 && controls.every(Boolean));
  await expect(thread.getByTestId('composer-input')).toHaveCount(0);
  checks.cacheBeforeSync = true;
  checks.cacheReadOnly = true;
  await page.screenshot({ path: join(artifacts, 'native-cached.png') });
  const synchronized = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/sessions/${owner.session_id}/bootstrap`);
  delay = false;
  release();
  const bootstrap = await payload(await synchronized);
  assert.equal(bootstrap.thread_id, owner.thread_id);
  assert.equal(bootstrap.session.workspace_id, owner.workspace_id);
  await expect(cached).toHaveCount(0);
  await expect(thread.getByTestId('composer-input')).toHaveValue(draft);
  await expect(thread.locator('.chat-stream-shell')).toContainText(answer);
  checks.liveAfterSync = true;
  const restartedImage = await probeImage(imageOwner, 'restart', 304);
  assert.equal(restartedImage.response.etag, coldImage.response.etag);
  assert.notEqual(restartedImage.response['x-request-id'], warmImage.response['x-request-id']);
  checks.imageCacheSurvivesRestart = true;
  await closeNative();

  const previousHeld = heldRequests.length;
  holdBootstrap();
  await launch(join(runtime, 'profile-b'));
  probeImage = await observeImageCache(page, imageReads);
  await page.locator(`button[data-session-id="${owner.session_id}"]`).dblclick();
  thread = threadSurface(page);
  await expect(thread).toBeVisible();
  await expect.poll(() => heldRequests.length).toBeGreaterThan(previousHeld);
  await expect(thread.locator('.native-thread-cached-history')).toHaveCount(0);
  await expect(thread.locator('.chat-turn')).toHaveCount(0);
  await expect(thread.locator('.chat-stream-empty-title')).toHaveText('正在加载最新 Turn');
  delay = false;
  release();
  await expect(thread.locator('.chat-stream-shell')).toContainText(answer);
  await expect(thread.getByTestId('composer-input')).toHaveValue('');
  checks.profileIsolation = true;
  await probeImage(imageOwner, 'independent-profile', 200);
  checks.imageProfileIsolation = true;
  await closeNative();
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(storageErrors(), [], '正常关窗与重启不能出现持久存储写入错误');
  assert.deepEqual(proxyErrors, []);
  await writeFile(join(artifacts, 'native-result.json'), JSON.stringify({ classification: 'Integration',
    checks, owner, modelCallIds, jobId: accepted.job_id, file, preferences, launches, heldRequests,
    receipts, imageReads, pageErrors, consoleErrors, storageErrors: storageErrors(), proxyErrors }, null, 2));
} catch (error) {
  failure = error;
} finally {
  const errors = failure ? [failure] : [];
  delay = false;
  release();
  if (failure && page && !page.isClosed()) {
    try { await page.screenshot({ path: join(artifacts, 'native-failure.png') }); }
    catch (error) { errors.push(error); }
  }
  if (failure) await writeFile(join(artifacts, 'native-diagnostics.json'), JSON.stringify({ error: String(failure),
    pageUrl: page?.url(), checks, owner, launches, heldRequests, receipts, pageErrors,
    consoleErrors, imageReads, storageErrors: storageErrors(), proxyErrors }, null, 2));
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, '便携 Electron 原生回归失败');
}
