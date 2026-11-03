import { writeFile, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL, resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const workspace = process.env.BOXTEAM_CACHE_WORKSPACE, other = process.env.BOXTEAM_CACHE_OTHER;
if (!baseUrl || !resultPath || !workspace || !other || !process.env.BOXTEAM_CACHE_UPDATE || !process.env.BOXTEAM_CACHE_ORIGINAL) throw new Error('文件缓存验收缺少隔离路径');
const run = TestRunContext.fromTestFile('tests/e2e/clients/web/test_quick_access_file_cache.py');
const result = { classification: 'E2E', phase: 'initialization', pageErrors: [], network: [], receipts: [] };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  page = await browser.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  const cdp = await page.context().newCDPSession(page), records = new Map();
  const record = id => { if (!records.has(id)) records.set(id, { id, encodedBodyBytes: 0 }); return records.get(id); };
  const selectedHeaders = headers => Object.fromEntries(Object.entries(headers).filter(([key]) =>
    ['if-none-match', 'etag', 'cache-control', 'vary', 'range', 'content-range', 'content-length',
      'content-type', 'x-request-id', 'x-boxteam-workspace-id'].includes(key.toLowerCase())).map(([key, value]) => [key.toLowerCase(), value]));
  cdp.on('Network.requestWillBeSent', event => {
    if (new URL(event.request.url).pathname !== '/api/v1/workspace/files/raw') return;
    const item = record(event.requestId);
    Object.assign(item, { url: event.request.url, phase: result.phase, request: selectedHeaders(event.request.headers) });
    result.network.push(item);
  });
  cdp.on('Network.requestWillBeSentExtraInfo', event => { record(event.requestId).wireRequest = selectedHeaders(event.headers); });
  cdp.on('Network.responseReceivedExtraInfo', event => { Object.assign(record(event.requestId), { wireStatus: event.statusCode, response: selectedHeaders(event.headers) }); });
  cdp.on('Network.dataReceived', event => { record(event.requestId).encodedBodyBytes += event.encodedDataLength; });
  cdp.on('Network.loadingFinished', event => { record(event.requestId).finished = true; });
  await cdp.send('Network.enable');
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (path, init = {}) => page.evaluate(async ({ path, init }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': token, 'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (body.request_id !== response.headers.get('x-request-id')) throw new Error('JSON request_id 不一致');
    return { status: response.status, body };
  }, { path, init });
  await page.goto(new URL('/api/gateway/health', baseUrl).href);
  const initial = await api('/api/gateway/workspaces'), routeA = initial.body.data.active_workspace_id;
  const registered = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({ root_path: other, name: 'Cache B' }) });
  expect(registered.status).toBe(200);
  const routeB = registered.body.data.items.find(item => item.name === 'Cache B').workspace_id;
  expect((await api(`/api/gateway/workspaces/${routeA}/activate`, { method: 'POST' })).status).toBe(200);
  await page.goto(baseUrl);
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  expect(health.status).toBe(200); expect(listing.status).toBe(200);
  result.phase = 'build-static-cache-policy';
  const manifestResponse = await fetch(new URL('/immutable-assets.json', baseUrl));
  expect(manifestResponse.status).toBe(200);
  const immutable = await manifestResponse.json();
  expect(immutable.length).toBeGreaterThan(0);
  result.staticResources = [];
  for (const path of ['/index.html', '/out/main.js', '/out/vs/editor/common/services/editorWebWorkerMain.js', '/' + immutable[0]]) {
    const cold = await fetch(new URL(path, baseUrl), { method: 'HEAD' });
    const policy = immutable.includes(path.slice(1)) ? 'public, max-age=31536000, immutable' : 'no-cache';
    expect(cold.status).toBe(200); expect(cold.headers.get('cache-control')).toBe(policy);
    const warm = await fetch(new URL(path, baseUrl), { headers: { 'If-None-Match': cold.headers.get('etag') } });
    expect(warm.status).toBe(304);
    const bytes = (await warm.arrayBuffer()).byteLength;
    expect(bytes).toBe(0); expect(warm.headers.get('cache-control')).toBe(policy);
    result.staticResources.push({ path, policy, warmStatus: warm.status, bytes });
  }
  const missingWorker = await fetch(new URL('/out/missing-worker.js', baseUrl));
  expect(missingWorker.status).toBe(404);
  result.staticCachePolicy = true;
  const targets = [];
  for (const route of [routeA, routeB]) {
    const current = await api('/api/v1/workspace', { headers: { 'X-BoxTeam-Workspace-Id': route } });
    const roots = await api('/api/v1/workspace/files/roots', { headers: { 'X-BoxTeam-Workspace-Id': route } });
    expect(current.status).toBe(200); expect(roots.status).toBe(200);
    targets.push({ route, gatewayId: health.body.data.gateway_id, workspaceId: current.body.data.workspace_id, rootUri: roots.body.data.workspace_uri });
  }
  result.targets = targets;
  const imageUri = target => target.rootUri + '/cache-image.png';
  const imageReads = target => result.network.filter(item => new URL(item.url).searchParams.get('path') === imageUri(target));
  const openImage = async (target, width) => {
    const uri = await page.evaluate(target => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: target.gatewayId,
      path: `/${target.workspaceId}/${encodeURIComponent(target.rootUri)}/cache-image.png` }).toString(), target);
    await command('_workbench.open', uri, [undefined, { pinned: true }]);
    const image = page.locator('.image-preview-editor').filter({ visible: true }).locator('img');
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate(element => [element.naturalWidth, element.naturalHeight])).toEqual([width, 1]);
  };
  const completeRead = async (target, after, status) => {
    await expect.poll(() => imageReads(target).slice(after).some(item => item.finished && item.wireStatus === status)).toBe(true);
    const item = imageReads(target).slice(after).find(item => item.finished && item.wireStatus === status);
    expect(item.response['cache-control']).toBe('private, no-cache');
    expect(item.response['x-request-id']).toBeTruthy();
    return item;
  };
  result.phase = 'original-image-cold';
  await openImage(targets[0], 1);
  const cold = await completeRead(targets[0], 0, 200);
  expect(cold.encodedBodyBytes).toBeGreaterThan(0);
  await command('workbench.action.closeActiveEditor');
  const beforeWarm = imageReads(targets[0]).length;
  result.phase = 'original-image-warm';
  await openImage(targets[0], 1);
  const warm = await completeRead(targets[0], beforeWarm, 304);
  // 原 watcher 建连可能额外再验证；每次暖读都必须零正文，不以请求数量代替字节合同。
  for (const item of imageReads(targets[0]).slice(beforeWarm)) {
    await expect.poll(() => item.finished && item.wireStatus).toBe(304);
    expect(item.encodedBodyBytes).toBe(0);
  }
  expect(warm.wireRequest['if-none-match']).toBe(cold.response.etag);
  expect(warm.encodedBodyBytes).toBe(0);
  expect(warm.response['x-request-id']).not.toBe(cold.response['x-request-id']);
  result.imageRevalidated = true;
  await command('workbench.action.closeActiveEditor');
  result.phase = 'same-path-other-owner';
  await openImage(targets[1], 1);
  const b = await completeRead(targets[1], 0, 200);
  expect(b.response.etag).not.toBe(cold.response.etag);
  result.ownerIsolated = true;
  await command('workbench.action.closeActiveEditor');
  result.phase = 'external-update-keeps-original-image-editor';
  await writeFile(join(workspace, 'cache-image.png'), Buffer.from(process.env.BOXTEAM_CACHE_UPDATE, 'base64'));
  const beforeUpdate = imageReads(targets[0]).length;
  await openImage(targets[0], 2);
  const updated = await completeRead(targets[0], beforeUpdate, 200);
  expect(updated.response.etag).not.toBe(cold.response.etag);
  result.updatedImage = true;
  const variants = await page.evaluate(async ({ target, workspace, etag }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const receipts = [];
    for (const [path, prefix, credential] of [
      ['cache-image.png', '/api/v1', token], [workspace + '/cache-image.png', '/api/v1', token],
      [target.rootUri + '/cache-image.png', `/api/v1/workspaces/${target.workspaceId}`, token],
      [target.rootUri + '/cache-image.png', `/api/v1/workspaces/${target.workspaceId}`, 'explicit-invalid-credential'],
    ]) {
      const response = await fetch(`${prefix}/workspace/files/raw?${new URLSearchParams({ path })}`, { headers: {
        'X-Local-Token': credential, 'X-BoxTeam-Workspace-Id': target.route, 'If-None-Match': etag,
      } });
      receipts.push({ status: response.status, etag: response.headers.get('etag'), policy: response.headers.get('cache-control'),
        requestId: response.headers.get('x-request-id'), bodyLength: (await response.arrayBuffer()).byteLength });
    }
    return receipts;
  }, { target: targets[0], workspace, etag: updated.response.etag });
  for (const item of variants.slice(0, 3)) {
    expect(item.status).toBe(304); expect(item.etag).toBe(updated.response.etag);
    expect(item.policy).toBe('private, no-cache'); expect(item.bodyLength).toBe(0); expect(item.requestId).toBeTruthy();
  }
  expect(variants[3].status).toBe(401); expect(variants[3].policy).toBe('no-store');
  result.inputVariants = variants;
  result.phase = 'watcher-updates-visible-original-image';
  const beforeWatcher = imageReads(targets[0]).length;
  await writeFile(join(workspace, 'cache-image.png'), Buffer.from(process.env.BOXTEAM_CACHE_ORIGINAL, 'base64'));
  const visibleImage = page.locator('.image-preview-editor').filter({ visible: true }).locator('img');
  await expect.poll(() => visibleImage.evaluate(element => [element.naturalWidth, element.naturalHeight])).toEqual([1, 1]);
  const watched = await completeRead(targets[0], beforeWatcher, 200);
  expect(watched.response.etag).not.toBe(updated.response.etag);
  result.watcherUpdatedImage = true;
  result.openingRequestCounts = Object.fromEntries(['original-image-cold', 'original-image-warm', 'same-path-other-owner']
    .map(phase => [phase, result.network.filter(item => item.phase === phase).length]));
  const raw = (target, name, headers = {}, endpoint = 'raw') => page.evaluate(async ({ target, name, headers, endpoint }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(`/api/v1/workspace/files/${endpoint}?${new URLSearchParams({ path: target.rootUri + '/' + name })}`,
      { headers: { 'X-Local-Token': token, 'X-BoxTeam-Workspace-Id': target.route, ...headers } });
    return { status: response.status, headers: Object.fromEntries(response.headers), bytes: [...new Uint8Array(await response.arrayBuffer())] };
  }, { target, name, headers, endpoint });
  result.phase = 'text-revalidation-and-save-cas';
  const original = await api(`/api/v1/workspace/files/content?${new URLSearchParams({ path: targets[0].rootUri + '/cache-text.txt' })}`,
    { headers: { 'X-BoxTeam-Workspace-Id': routeA } });
  expect(original.status).toBe(200);
  const textCold = await raw(targets[0], 'cache-text.txt'), textWarm = await raw(targets[0], 'cache-text.txt');
  expect(new TextDecoder().decode(new Uint8Array(textWarm.bytes))).toBe('原文件 A 🧪\n');
  expect(textWarm.bytes).toEqual(textCold.bytes);
  const textReads = result.network.filter(item => new URL(item.url).searchParams.get('path') === targets[0].rootUri + '/cache-text.txt');
  await expect.poll(() => textReads.at(-1)?.wireStatus).toBe(304);
  expect(textReads.at(-1).encodedBodyBytes).toBe(0);
  result.textRevalidated = true;
  await writeFile(join(workspace, 'cache-text.txt'), 'external change 🧪\n');
  const changed = await raw(targets[0], 'cache-text.txt');
  expect(new TextDecoder().decode(new Uint8Array(changed.bytes))).toBe('external change 🧪\n');
  const conflict = await api(`/api/v1/workspace/files/content?${new URLSearchParams({ path: targets[0].rootUri + '/cache-text.txt' })}`,
    { method: 'PUT', headers: { 'X-BoxTeam-Workspace-Id': routeA }, body: JSON.stringify({ content: 'stale save', expected_revision: original.body.data.revision }) });
  expect(conflict.status).toBe(409); result.staleSaveRejected = true;
  result.phase = 'range-download-and-failure-boundaries';
  const range = await raw(targets[0], 'cache-image.png', { Range: 'bytes=1-3', 'If-None-Match': watched.response.etag });
  expect(range.status).toBe(206); expect(range.bytes).toEqual([80, 78, 71]);
  expect(range.headers['cache-control']).toBe('no-store'); expect(range.headers['content-range']).toMatch(/^bytes 1-3\/\d+$/);
  const download = await raw(targets[0], 'cache-image.png', { 'If-None-Match': watched.response.etag }, 'download');
  expect(download.status).toBe(200); expect(download.headers['cache-control']).toBe('no-store');
  const denied = await raw(targets[0], 'cache-image.png', { 'X-Local-Token': 'explicit-invalid-credential', 'If-None-Match': watched.response.etag });
  expect(denied.status).toBe(401); expect(denied.headers['cache-control']).toBe('no-store');
  const wrongOwner = await raw(targets[0], 'cache-image.png', { 'X-BoxTeam-Workspace-Id': routeB, 'If-None-Match': watched.response.etag });
  expect(wrongOwner.status).toBe(400); expect(wrongOwner.headers['cache-control']).toBe('no-store');
  await unlink(join(workspace, 'cache-image.png'));
  const missing = await raw(targets[0], 'cache-image.png');
  expect(missing.status).toBe(404); expect(missing.headers['cache-control']).toBe('no-store');
  result.receipts = { textCold, textWarm, changed, conflict, range, download, denied, wrongOwner, missing };
  result.errorsNoStore = true;
  result.phase = 'passed';
} catch (error) {
  failure = error; result.error = String(error?.stack ?? error);
  if (page) await page.screenshot({ path: join(run.artifactsDir, 'file-cache-failure.png') }).catch(error => { result.screenshotError = String(error); });
} finally {
  await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
