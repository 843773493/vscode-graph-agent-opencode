import { readFile, writeFile, unlink } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL, resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const assets = process.env.BOXTEAM_STATIC_UPGRADE_ASSETS;
const runtime = process.env.BOXTEAM_BROWSER_RUNTIME_PATH;
if (!baseUrl || !resultPath || !assets || !runtime) throw new Error('静态升级验收缺少隔离参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_static_upgrade.py');
const main = '/out/main.js', worker = '/out/vs/editor/common/services/editorWebWorkerMain.js';
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], network: [],
  boundary: '正式 Workbench 副本仅追加可观测版本标记；真实 Gateway、后端及 Chromium，未替换 HTTP 响应或缓存' };
let page, failure, chunk;
try {
  const context = await chromium.launchPersistentContext(join(runtime, 'profile'), {
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  const cdp = await context.newCDPSession(page), records = new Map();
  const record = id => {
    if (!records.has(id)) records.set(id, { id, bytes: 0 });
    return records.get(id);
  };
  const headers = values => Object.fromEntries(Object.entries(values)
    .filter(([key]) => ['if-none-match', 'etag', 'cache-control', 'content-type', 'content-length'].includes(key.toLowerCase()))
    .map(([key, value]) => [key.toLowerCase(), value]));
  cdp.on('Network.requestWillBeSent', event => {
    const path = new URL(event.request.url).pathname;
    if (!chunk && path.startsWith('/out/chunks/') && path.endsWith('.js')) chunk = path;
    if (!['/', main, worker, chunk].includes(path)) return;
    const item = record(event.requestId);
    Object.assign(item, { path, phase: result.phase }); result.network.push(item);
  });
  cdp.on('Network.requestWillBeSentExtraInfo', event => { record(event.requestId).request = headers(event.headers); });
  cdp.on('Network.responseReceivedExtraInfo', event => {
    Object.assign(record(event.requestId), { status: event.statusCode, response: headers(event.headers) });
  });
  cdp.on('Network.responseReceived', event => {
    const item = record(event.requestId);
    item.response ??= headers(event.response.headers);
    item.status ??= event.response.status;
    item.fromDiskCache = event.response.fromDiskCache;
  });
  cdp.on('Network.requestServedFromCache', event => { record(event.requestId).servedFromCache = true; });
  cdp.on('Network.dataReceived', event => { record(event.requestId).bytes += event.encodedDataLength; });
  cdp.on('Network.loadingFinished', event => { record(event.requestId).finished = true; });
  await cdp.send('Network.enable');
  const ready = async version => {
    await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
    expect(await page.evaluate(() => globalThis.__boxteamStaticUpgrade)).toBe(version);
    expect(await page.evaluate(path => new Promise((resolve, reject) => {
      const instance = new Worker(path, { type: 'module', name: 'boxteam-static-upgrade' });
      const timer = setTimeout(() => { instance.terminate(); reject(new Error('原 Worker 未发布版本')); }, 5000);
      const close = () => { clearTimeout(timer); instance.terminate(); };
      instance.onmessage = event => {
        if (!event.data?.boxteamStaticUpgrade) return;
        close(); resolve(event.data.boxteamStaticUpgrade);
      };
      instance.onerror = event => { event.preventDefault(); close(); reject(new Error(event.message)); };
    }), worker)).toBe(version);
  };
  result.phase = 'cold';
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' }); await ready('version-1');
  expect(chunk, '主入口实际加载内容地址分块').toBeTruthy();
  const coldChunk = result.network.find(item => item.phase === 'cold' && item.path === chunk);
  await expect.poll(() => coldChunk?.finished).toBe(true);
  expect(coldChunk.status).toBe(200); expect(coldChunk.bytes).toBeGreaterThan(0);
  expect(coldChunk.response['cache-control']).toBe('public, max-age=31536000, immutable');
  result.initialization = await page.evaluate(async () => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    return Promise.all(['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace'].map(async path => {
      const response = await fetch(path, { headers: { 'X-Local-Token': token } });
      const body = await response.json();
      if (response.status !== 200 || !body.request_id || body.request_id !== response.headers.get('X-Request-ID')) {
        throw new Error('初始化失败：' + path);
      }
      return { path, requestId: body.request_id };
    }));
  });
  result.phase = 'warm';
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready('version-1');
  await expect.poll(() => result.network.some(item => item.phase === 'warm' && item.path === worker && item.status === 304)).toBe(true);
  await expect.poll(() => result.network.some(item => item.phase === 'warm' && item.path === main && item.finished && item.status === 304)).toBe(true);
  const warmMain = result.network.find(item => item.phase === 'warm' && item.path === main);
  expect(warmMain.request['if-none-match']).toBeTruthy(); expect(warmMain.bytes).toBe(0);
  expect(warmMain.response['cache-control']).toBe('no-cache');
  const warmChunk = result.network.find(item => item.phase === 'warm' && item.path === chunk);
  await expect.poll(() => warmChunk?.finished).toBe(true);
  expect(warmChunk.fromDiskCache || warmChunk.servedFromCache).toBe(true);
  expect(warmChunk.bytes).toBe(0);
  result.immutableChunkReuse = { path: chunk, coldBytes: coldChunk.bytes, warmBytes: warmChunk.bytes };
  result.warmRevalidation = true;
  result.phase = 'upgrade';
  for (const path of [main, worker]) {
    const file = join(assets, path.slice(1));
    const old = await readFile(file, 'utf8');
    const marker = path === main ? '__boxteamStaticUpgrade="version-1"' : 'boxteamStaticUpgrade:"version-1"';
    expect(old).toContain(marker);
    await writeFile(file, old.replace(marker, marker.replace('version-1', 'version-two')));
  }
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready('version-two');
  await expect.poll(() => result.network.some(item => item.phase === 'upgrade' && item.path === main && item.finished && item.status === 200)).toBe(true);
  const upgraded = result.network.find(item => item.phase === 'upgrade' && item.path === main);
  expect(upgraded.request['if-none-match']).toBe(warmMain.response.etag);
  expect(upgraded.response.etag).not.toBe(warmMain.response.etag); expect(upgraded.bytes).toBeGreaterThan(0);
  expect(upgraded.response['cache-control']).toBe('no-cache'); result.upgradeExecuted = true;
  result.phase = 'missing-worker';
  await unlink(join(assets, worker.slice(1)));
  const workerFailure = await page.evaluate(path => new Promise((resolve, reject) => {
    const instance = new Worker(path, { type: 'module', name: 'boxteam-static-upgrade' });
    const timer = setTimeout(() => { instance.terminate(); reject(new Error('缺失 Worker 没有失败')); }, 5000);
    instance.onmessage = event => {
      if (!event.data?.boxteamStaticUpgrade) return;
      clearTimeout(timer); instance.terminate(); reject(new Error('已删除 Worker 仍执行旧正文'));
    };
    instance.onerror = event => { event.preventDefault(); clearTimeout(timer); instance.terminate(); resolve(true); };
  }), worker);
  expect(workerFailure).toBe(true);
  const missing = await page.request.get(new URL(worker, baseUrl).href);
  expect(missing.status()).toBe(404); expect(missing.headers()['content-type']).not.toContain('text/html');
  result.missingWorkerRejected = true; expect(result.pageErrors).toEqual([]); result.phase = 'passed';
} catch (error) {
  failure = error; result.error = String(error);
  if (page) {
    try { await page.screenshot({ path: join(dirname(resultPath), 'static-upgrade-failure.png') }); }
    catch (captureError) { failure = new AggregateError([failure, captureError], '升级验收和截图失败'); }
  }
} finally {
  try { await writeFile(resultPath, JSON.stringify(result, null, 2)); }
  catch (saveError) { failure = failure ? new AggregateError([failure, saveError], '升级验收和原件保存失败') : saveError; }
  try { await run.close(); }
  catch (closeError) { failure = failure ? new AggregateError([failure, closeError], '升级验收和清理失败') : closeError; }
  if (failure) throw failure;
}
