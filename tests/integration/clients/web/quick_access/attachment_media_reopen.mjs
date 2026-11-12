// Integration：真实消息上传与规范 URI、原图片 Pane 和官方隔离音视频 Webview。
import { writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`附件媒体验收缺少 ${name}`); return value; };
const base = required('BOXTEAM_BROWSER_BASE_URL'), output = required('BOXTEAM_BROWSER_RESULT_PATH');
const [a, b] = JSON.parse(required('BOXTEAM_ATTACHMENT_MEDIA_FIXTURE'));
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_attachment_media_reopen.py');
const result = { phase: 'initialize', classification: 'Integration', pageErrors: [], consoleErrors: [], rawReads: [], workerReads: [],
  copied: [], media: [], addressRequests: [], executions: [], fileWrites: [],
  boundary: '真实Gateway／Backend／正式Workbench，媒体随原消息API上传、各执行一次受控模型，浏览器阶段零执行。图片沿原ImagePreviewEditor，音视频沿官方隔离Webview与Worker，未替换媒体或网络响应。' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('附件媒体 Chromium', () => browser.close());
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'], viewport: { width: 1700, height: 1000 } });
  run.addCleanup('附件媒体 BrowserContext', () => context.close());
  page = await context.newPage(); page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/resource-addresses/parse') result.addressRequests.push({ phase: result.phase, body: request.postDataJSON() });
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executions.push(path);
    if (/^\/api\/v1\/workspace\/files\//.test(path) && ['PUT', 'DELETE'].includes(request.method())) result.fileWrites.push(path);
  });
  page.on('response', response => {
    const request = response.request(), url = new URL(response.url());
    if (url.pathname === '/api/v1/workspace/files/raw') {
      result.rawReads.push({ phase: result.phase, path: url.searchParams.get('path'), method: request.method(), status: response.status(),
        gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'],
        range: request.headers().range ?? null, requestId: response.headers()['x-request-id'],
        mime: response.headers()['content-type']?.split(';')[0], filename: response.headers()['content-disposition'] });
    } else if (url.hostname.endsWith('.vscode-cdn.net') && url.pathname.endsWith('/original/content')) {
      result.workerReads.push({ phase: result.phase, url: url.href, status: response.status(),
        mime: response.headers()['content-type']?.split(';')[0], fromServiceWorker: response.fromServiceWorker() });
    }
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (path, owner) => page.evaluate(async ({ path, owner }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { headers: { 'X-Local-Token': credential,
      ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } : {}) } });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, payload }));
    return payload.data;
  }, { path, owner });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const ready = () => expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await ready();
  expect((await api('/api/gateway/health')).gateway_id).toBe(a.gatewayId);
  expect((await api('/api/gateway/workspaces?check_health=false')).active_workspace.workspace_id).toBe(a.workspaceId);
  expect((await api('/api/v1/workspace', a)).workspace_id).toBe(a.workspaceId);
  await command('workbench.action.closeAllEditors');
  await page.locator(`button[data-session-id="${a.sessionId}"]`).dblclick();
  await expect(page.locator(`section.boxteam-thread[data-session-id="${a.sessionId}"] textarea[data-testid="composer-input"]`))
    .toBeEnabled({ timeout: 30_000 });
  const quick = page.locator('.quick-input-widget');
  const references = new Map();
  result.phase = 'copy-candidates';
  const tabs = await page.locator('.tabs-container .tab').count(), reads = result.rawReads.length;
  await command('boxteam.quickAccess.resources', { gatewayId: a.gatewayId, workspaceId: a.workspaceId });
  for (const item of a.attachments) {
    await quick.locator('input').first().fill(`resource ${item.name}`);
    const row = quick.locator('.monaco-list-row').filter({ hasText: item.name });
    await expect(row).toBeVisible();
    // 候选先出现，才能据原加载标记等待其余来源完成，避免检查尚未挂载的空列表。
    await expect(quick.locator('[data-quick-input-id="source-pending"]')).toHaveCount(0);
    result.copyCandidate = { item, id: await row.locator('[data-quick-input-id]').getAttribute('data-quick-input-id'), text: await row.innerText() };
    await row.hover();
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/resource-addresses/parse'
        && response.request().postDataJSON()?.uri === item.file_id),
      row.getByRole('button', { name: '复制资源 URI', exact: true }).click(),
    ]);
    const payload = await response.json();
    expect(response.status()).toBe(200); expect(payload.request_id).toBe(response.headers()['x-request-id']);
    expect(payload.data).toMatchObject({ gateway_id: a.gatewayId, scope_id: a.workspaceId, kind: 'attachments', session_id: null, thread_id: null });
    const uri = payload.data.reference_uri;
    expect(uri).toContain(`boxteam://${a.gatewayId}/workspace/${a.workspaceId}/resources/attachments/att_`);
    expect(uri.split('/resources/attachments/')[1]).toBe(item.file_id.split('/resources/attachments/')[1]);
    expect(uri.endsWith('/original/content')).toBe(true);
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(uri);
    references.set(item.content_type, uri); result.copied.push({ item, uri });
    await expect(quick).toBeVisible();
    expect(result.rawReads).toHaveLength(reads);
    expect(await page.locator('.tabs-container .tab').count()).toBe(tabs);
  }
  result.copiedWithoutRead = true;
  await page.keyboard.press('Escape');
  await command('workbench.action.closeAllEditors');
  const open = async (uri, modifiers = []) => {
    await command('workbench.action.quickOpen', uri);
    await quick.locator(`[data-quick-input-id="link:${uri}"]`).click({ modifiers });
    await expect(quick).not.toBeVisible();
  };
  const reload = async () => {
    await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
    await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  };
  const image = async () => {
    const element = page.locator('.image-preview-editor img');
    await expect(element).toBeVisible();
    await expect.poll(() => element.evaluate(image => image.complete && image.naturalWidth > 0 && image.naturalHeight > 0)).toBe(true);
    const state = await element.evaluate(async image => ({ src: image.currentSrc, width: image.naturalWidth, height: image.naturalHeight,
      mime: (await fetch(image.currentSrc)).headers.get('content-type') }));
    expect(state.mime).toBe('image/jpeg'); result.media.push({ phase: result.phase, kind: 'image', ...state });
  };
  const media = async (owner, kind, duration) => {
    let locator;
    await expect.poll(async () => {
      const found = [];
      for (const frame of page.frames()) {
        const element = frame.locator(`${kind}[src*="/${owner.workspaceId}/"]`);
        if (await element.count()) found.push(element);
      }
      if (found.length !== 1) return false;
      locator = found[0];
      return locator.evaluate(element => element.readyState >= 2 && element.error === null);
    }, { timeout: 30_000 }).toBe(true);
    const state = await locator.evaluate(element => ({ src: element.currentSrc, origin: location.origin,
      duration: element.duration, width: element.videoWidth, height: element.videoHeight }));
    expect(new URL(state.origin).hostname).toMatch(/\.vscode-cdn\.net$/);
    expect(state.origin).not.toBe(new URL(base).origin);
    expect(state.duration).toBeCloseTo(duration, 3);
    if (kind === 'video') { expect(state.width).toBe(320); expect(state.height).toBe(180); }
    result.media.push({ phase: result.phase, owner, kind, ...state });
  };
  const copyEditor = async uri => {
    const before = result.rawReads.length;
    await command('boxteam.resource.copyUri');
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(uri);
    expect(result.rawReads).toHaveLength(before);
  };
  const imageUri = references.get('image/jpeg');
  result.phase = 'image'; await open(imageUri); await image(); result.imageDecoded = true;
  await copyEditor(imageUri);
  result.phase = 'image-restore'; await reload(); await image(); result.imageRestored = true;
  await command('workbench.action.closeAllEditors');
  result.phase = 'image-reopen'; await open(imageUri); await image(); result.imageReopened = true;
  await command('workbench.action.closeAllEditors');
  const aAudio = references.get('audio/wav'), bItem = b.attachments.find(item => item.content_type === 'audio/wav');
  const bAddress = await page.evaluate(async ({ fileId, gatewayId }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch('/api/gateway/resource-addresses/parse', { method: 'POST',
      headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json' },
      body: JSON.stringify({ uri: fileId, local_gateway_id: gatewayId, resource_gateway_id: gatewayId }) });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return body.data;
  }, { fileId: bItem.file_id, gatewayId: b.gatewayId });
  result.phase = 'audio-owners';
  await open(aAudio); await media(a, 'audio', 0.5);
  await open(bAddress.reference_uri, ['Alt']); await media(b, 'audio', 1); await media(a, 'audio', 0.5);
  await expect(page.locator('.editor-group-container')).toHaveCount(2); result.audioOwnersIsolated = true;
  await copyEditor(bAddress.reference_uri);
  result.phase = 'audio-restore'; await reload(); await media(a, 'audio', 0.5); await media(b, 'audio', 1); result.audioRestored = true;
  await command('workbench.action.closeAllEditors'); await command('workbench.action.joinAllGroups');
  result.phase = 'audio-reopen'; await open(bAddress.reference_uri); await media(b, 'audio', 1); result.audioReopened = true;
  await command('workbench.action.closeAllEditors');
  const videoUri = references.get('video/mp4');
  result.phase = 'video'; await open(videoUri); await media(a, 'video', 3); result.videoDecoded = true;
  await copyEditor(videoUri);
  result.phase = 'video-restore'; await reload(); await media(a, 'video', 3); result.videoRestored = true;
  await command('workbench.action.closeAllEditors');
  result.phase = 'video-reopen'; await open(videoUri); await media(a, 'video', 3); result.videoReopened = true;
  for (const owner of [a, b]) for (const item of owner.attachments) {
    const tail = '/resources/attachments/' + item.file_id.split('/resources/attachments/')[1];
    const reads = result.rawReads.filter(read => read.path.endsWith(tail));
    expect(reads.some(read => read.method === 'HEAD')).toBe(true);
    expect(reads.some(read => read.method === 'GET')).toBe(true);
    for (const read of reads) {
      expect(read.gatewayId).toBe(owner.gatewayId); expect(read.workspaceId).toBe(owner.workspaceId);
      expect(read.requestId).toBeTruthy(); expect(read.mime).toBe(item.content_type);
      expect(decodeURIComponent(read.filename)).toContain(item.name);
      expect(read.status).toBe(read.range ? 206 : 200);
    }
  }
  for (const mime of ['audio/wav', 'video/mp4']) {
    expect(result.workerReads.some(read => read.mime === mime && read.fromServiceWorker)).toBe(true);
  }
  expect(result.pageErrors).toEqual([]); expect(result.executions).toEqual([]); expect(result.fileWrites).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error instanceof Error ? error.stack : String(error);
  if (page) {
    result.visibleRows = await page.locator('.quick-input-widget .monaco-list-row').allTextContents();
    result.notifications = await page.locator('.notification-toast').allTextContents();
    await page.screenshot({ path: join(dirname(output), 'attachment-media-failure.png') }).catch(cause => { result.screenshotError = String(cause); });
  }
} finally {
  const finalized = await Promise.allSettled([writeFile(output, JSON.stringify(result, null, 2) + '\n'), run.close()]);
  const errors = finalized.filter(item => item.status === 'rejected').map(item => item.reason);
  if (errors.length) throw new AggregateError([...(failure ? [failure] : []), ...errors], '附件媒体验收或产物清理失败');
}
if (failure) throw failure;
