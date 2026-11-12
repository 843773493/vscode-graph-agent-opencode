import { writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`媒体验收缺少 ${name}`); return value; };
const base = required('BOXTEAM_BROWSER_BASE_URL'), output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_media_reopen.py');
const result = { phase: 'initialize', classification: 'Integration', pageErrors: [], consoleErrors: [], rawReads: [], addresses: [], executions: [], fileWrites: [], media: [],
  boundary: '真实Gateway／Backend／正式Workbench及官方隔离Webview；模型回放仅用于启动且零调用，普通WAV与原视频fixture为输入，不替换媒体、Worker或资源响应。' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('媒体 Chromium', () => browser.close());
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'], viewport: { width: 1700, height: 1000 } });
  run.addCleanup('媒体 BrowserContext', () => context.close());
  page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executions.push(path);
    if (/^\/api\/v1\/workspace\/files\//.test(path) && ['PUT', 'DELETE'].includes(request.method())) result.fileWrites.push(path);
  });
  page.on('response', response => {
    const request = response.request(), url = new URL(request.url());
    if (url.pathname !== '/api/v1/workspace/files/raw') return;
    result.rawReads.push({ phase: result.phase, path: url.searchParams.get('path'), status: response.status(),
      gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'],
      range: request.headers().range ?? null, requestId: response.headers()['x-request-id'] });
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = async (path, owner, body) => {
    return page.evaluate(async ({ path, owner, body }) => {
      const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
        headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json', ...(owner ? {
          'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
        } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
      const payload = await response.json();
      if (!response.ok || payload.code !== 0 || payload.request_id !== response.headers.get('X-Request-ID')) {
        throw new Error(JSON.stringify({ path, status: response.status, payload }));
      }
      return payload.data;
    }, { path, owner, body });
  };
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const ready = () => expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await ready();
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces?check_health=false');
  const a = { gatewayId: health.gateway_id, workspaceId: workspaces.active_workspace.workspace_id };
  expect((await api('/api/v1/workspace', a)).workspace_id).toBe(a.workspaceId);
  const registered = await api('/api/gateway/managed-workspaces', undefined,
    { root_path: required('BOXTEAM_MEDIA_OTHER_WORKSPACE'), name: 'Media B' });
  const target = registered.items.find(item => item.name === 'Media B');
  const b = { gatewayId: target.gateway_id, workspaceId: target.workspace_id };
  expect(a.workspaceId).not.toBe(b.workspaceId);
  const roots = await Promise.all([a, b].map(owner => api('/api/v1/workspace/files/roots', owner)));
  const uris = await Promise.all([a, b].map(async (owner, index) => {
    const address = await api('/api/gateway/resource-addresses/parse', undefined,
      { uri: roots[index].workspace_uri + '/same-audio.wav', local_gateway_id: a.gatewayId, resource_gateway_id: owner.gatewayId });
    expect(address.scope_id).toBe(owner.workspaceId); return address.reference_uri;
  }));
  result.owners = [a, b]; result.audioUris = uris;
  const quick = page.locator('.quick-input-widget');
  const open = async (uri, modifiers = []) => {
    await command('workbench.action.quickOpen', uri);
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/resource-addresses/parse'
        && response.request().postDataJSON()?.uri === uri),
      quick.locator(`[data-quick-input-id="link:${uri}"]`).click({ modifiers }),
    ]);
    const payload = await response.json();
    expect(response.status()).toBe(200); expect(payload.request_id).toBe(response.headers()['x-request-id']);
    result.addresses.push({ phase: result.phase, uri, requestId: payload.request_id, address: payload.data });
    await expect(quick).not.toBeVisible();
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
      duration: element.duration, readyState: element.readyState, width: element.videoWidth, height: element.videoHeight }));
    expect(state.origin).not.toBe(new URL(base).origin); expect(new URL(state.origin).hostname).toMatch(/\.vscode-cdn\.net$/);
    expect(state.src).toContain(`/${owner.workspaceId}/`);
    if (duration !== undefined) expect(state.duration).toBeCloseTo(duration, 3);
    else { expect(state.duration).toBeGreaterThan(0); expect(state.width).toBeGreaterThan(0); expect(state.height).toBeGreaterThan(0); }
    result.media.push({ phase: result.phase, owner, kind, ...state });
  };
  const copy = async uri => {
    const before = result.rawReads.length;
    await command('boxteam.resource.copyUri');
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(uri);
    expect(result.rawReads).toHaveLength(before);
  };
  const reload = async () => {
    await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
    await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  };
  await command('workbench.action.closeAllEditors');
  result.phase = 'audio-two-owners';
  await open(uris[0]); await media(a, 'audio', 0.5);
  await open(uris[1], ['Alt']); await media(b, 'audio', 1); await media(a, 'audio', 0.5);
  await expect(page.locator('.editor-group-container')).toHaveCount(2);
  result.audioOwnersIsolated = true;
  result.phase = 'copy-audio'; await copy(uris[1]);
  result.phase = 'restore-audio'; await reload();
  await media(a, 'audio', 0.5); await media(b, 'audio', 1); result.audioRestored = true;
  await command('workbench.action.closeAllEditors'); await command('workbench.action.joinAllGroups');
  result.phase = 'reopen-copied-audio'; await open(uris[1]); await media(b, 'audio', 1); result.audioReopened = true;
  const video = await api('/api/gateway/resource-addresses/parse', undefined,
    { uri: roots[0].workspace_uri + '/assets/multimodal-test.mp4', local_gateway_id: a.gatewayId, resource_gateway_id: a.gatewayId });
  result.phase = 'video-original'; await open(video.reference_uri); await media(a, 'video'); result.videoDecoded = true;
  result.phase = 'copy-video'; await copy(video.reference_uri);
  await command('workbench.action.closeAllEditors');
  result.phase = 'reopen-copied-video'; await open(video.reference_uri); await media(a, 'video'); result.videoReopened = true;
  result.phase = 'restore-video'; await reload(); await media(a, 'video'); result.videoRestored = true;
  expect(result.rawReads.length).toBeGreaterThan(0);
  for (const read of result.rawReads) {
    const owner = [a, b].find(owner => owner.workspaceId === read.workspaceId && owner.gatewayId === read.gatewayId);
    expect(owner).toBeTruthy(); expect(read.path).toContain(`/workspace/${owner.workspaceId}/`); expect(read.requestId).toBeTruthy();
    expect(read.status).toBe(read.range ? 206 : 200);
  }
  expect(result.pageErrors).toEqual([]); expect(result.executions).toEqual([]); expect(result.fileWrites).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error instanceof Error ? error.stack : String(error);
  if (page) await page.screenshot({ path: join(dirname(output), 'media-failure.png') }).catch(cause => { result.screenshotError = String(cause); });
} finally {
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
