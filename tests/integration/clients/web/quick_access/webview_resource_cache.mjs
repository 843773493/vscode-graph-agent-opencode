// Integration：原 Markdown 扩展及隔离 Webview，真实 Gateway／FileService，无响应替身。
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  if (!process.env[name]) throw new Error(`缺少环境变量 ${name}`);
  return process.env[name];
};
const fixture = JSON.parse(required('BOXTEAM_WEBVIEW_FIXTURE'));
const artifacts = dirname(required('BOXTEAM_BROWSER_RESULT_PATH'));
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_webview_resource_cache.py');
const result = { classification: 'Integration', phase: 'initialize', routeStubs: [],
  pageErrors: [], consoleErrors: [], failedRequests: [], requests: [], responses: [], cacheEntries: [], frames: [] };
const selectedHeaders = headers => Object.fromEntries(Object.entries(headers).filter(([name]) =>
  ['if-none-match', 'etag', 'cache-control', 'content-type', 'content-length', 'x-request-id',
    'x-boxteam-target-gateway-id', 'x-boxteam-workspace-id'].includes(name.toLowerCase())));
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('webview-browser', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('webview-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', message => {
    if (message.type() === 'error') result.consoleErrors.push({ text: message.text(), location: message.location() });
  });
  context.on('requestfailed', request => result.failedRequests.push({
    phase: result.phase, url: request.url(), failure: request.failure(),
  }));
  page.on('framenavigated', frame => result.frames.push({ phase: result.phase, url: frame.url() }));
  context.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/workspace/files/raw' || url.pathname.endsWith('/resources/navigation')) {
      result.requests.push({ phase: result.phase, url: request.url(), method: request.method(),
        headers: selectedHeaders(request.headers()), body: request.postData() });
    }
  });
  context.on('response', response => {
    const url = response.url();
    if (!url.includes('/api/v1/workspace/files/raw') && !url.includes('.vscode-resource.')) return;
    result.responses.push({ phase: result.phase, url, status: response.status(),
      fromServiceWorker: response.fromServiceWorker(), headers: selectedHeaders(response.headers()) });
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 45000 });
  const uri = await page.evaluate(fixture => globalThis.workbench.URI.from({ scheme: 'boxteam-file',
    authority: fixture.gatewayId,
    path: `/${fixture.workspaceId}/${encodeURIComponent(fixture.rootUri)}/webview-cache/preview.md` }).toString(), fixture);
  result.phase = 'open-original-preview';
  await command('_workbench.open', uri, [undefined, { pinned: true }]);
  await command('markdown.showPreview');
  const body = page.frameLocator('iframe.webview:visible').frameLocator('iframe#active-frame').locator('body');
  const probe = body.locator('.cache-probe');
  await expect(probe).toHaveText('原文件 CSS 与字体', { timeout: 60000 });
  await expect(probe).toHaveCSS('color', 'rgb(17, 34, 51)');
  await expect(probe).toHaveCSS('background-color', 'rgb(90, 91, 92)');
  await probe.evaluate(async element => {
    await element.ownerDocument.fonts.load('16px CacheProbe');
    if (!element.ownerDocument.fonts.check('16px CacheProbe')) throw new Error('原 Webview 字体未加载');
  });
  for (const resource of ['markdown-math/notebook-out/katex.min.css',
    'markdown-math/preview-styles/index.css', 'mermaid-markdown-features/markdown-preview-out/index.js']) {
    await expect.poll(() => result.responses.some(item => item.phase === 'open-original-preview'
      && item.url.includes(`/extensions/${resource}`) && item.fromServiceWorker && item.status === 200)).toBe(true);
  }
  expect(result.failedRequests.filter(item => new URL(item.url).pathname.startsWith('/extensions/'))).toEqual([]);
  const cssUri = fixture.rootUri + '/webview-cache/style.css';
  const fontUri = fixture.rootUri + '/webview-cache/font.ttf';
  const retainedUri = fixture.rootUri + '/webview-cache/retained.css';
  const raw = uri => result.requests.filter(item => new URL(item.url).pathname === '/api/v1/workspace/files/raw'
    && new URL(item.url).searchParams.get('path') === uri);
  expect(raw(cssUri).length).toBeGreaterThan(0);
  expect(raw(fontUri).length).toBeGreaterThan(0);
  const outer = page.frames().find(frame => frame.url().includes('.vscode-cdn.net/') && frame.url().includes('/index.html?'));
  expect(outer).toBeTruthy();
  const inspectCache = () => outer.evaluate(async () => {
    const entries = [];
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      for (const request of await cache.keys()) {
        const response = await cache.match(request);
        entries.push({ name, url: request.url, etag: response.headers.get('etag'),
          mime: response.headers.get('content-type'), bytes: (await response.arrayBuffer()).byteLength });
      }
    }
    return entries;
  });
  await expect.poll(async () => (await inspectCache()).some(item =>
    item.url.includes('/webview-cache/style.css') && item.etag && item.bytes > 0)).toBe(true);
  await expect.poll(async () => (await inspectCache()).some(item =>
    item.url.includes('/webview-cache/font.ttf') && item.etag && item.bytes > 0)).toBe(true);
  await expect.poll(async () => (await inspectCache()).some(item =>
    item.url.includes('/webview-cache/retained.css') && item.etag && item.bytes > 0)).toBe(true);
  result.cacheEntries = await inspectCache();
  expect(result.cacheEntries.find(item => item.url.includes('/webview-cache/font.ttf'))?.mime).toBe('font/ttf');
  result.phase = 'warm-preview';
  const before = { css: raw(cssUri).length, font: raw(fontUri).length };
  await command('markdown.preview.refresh');
  await expect(probe).toHaveCSS('color', 'rgb(17, 34, 51)');
  await expect.poll(() => result.responses.some(item => item.phase === 'warm-preview'
    && item.url.includes('/webview-cache/style.css') && item.fromServiceWorker)).toBe(true);
  await probe.evaluate(element => element.ownerDocument.fonts.load('16px CacheProbe').then(() => undefined));
  expect(raw(cssUri)).toHaveLength(before.css);
  expect(raw(fontUri)).toHaveLength(before.font);
  expect(result.requests.some(item => item.phase === 'warm-preview' && item.body
    && JSON.parse(item.body).uri === cssUri && JSON.parse(item.body).operation === 'describe')).toBe(true);
  result.phase = 'updated-preview';
  const style = join(fixture.directory, 'style.css');
  await writeFile(style, (await readFile(style, 'utf8')).replace('rgb(17, 34, 51)', 'rgb(61, 72, 83)'));
  await command('markdown.preview.refresh');
  await expect(probe).toHaveCSS('color', 'rgb(61, 72, 83)');
  expect(raw(cssUri).length).toBeGreaterThan(before.css);
  result.phase = 'evicted-resource-entries';
  const originalEtag = result.cacheEntries.find(item => item.url.includes('/webview-cache/style.css')).etag;
  await expect.poll(async () => {
    const current = (await inspectCache()).find(item => item.url.includes('/webview-cache/style.css'));
    return Boolean(current?.etag && current.etag !== originalEtag);
  }).toBe(true);
  const stored = await inspectCache();
  const evicted = stored.filter(item => item.url.includes('/webview-cache/style.css')
    || item.url.includes('/webview-cache/font.ttf'));
  expect(evicted).toHaveLength(2);
  // 只模拟引擎丢弃两条可再生资源；其它扩展资源与工作台存储均保持原件。
  const removed = await outer.evaluate(async entries => {
    const removed = [];
    for (const entry of entries) {
      if (await (await caches.open(entry.name)).delete(entry.url)) removed.push(entry.url);
    }
    return removed;
  }, evicted);
  expect(removed).toEqual(evicted.map(item => item.url));
  const retained = stored.filter(item => !removed.includes(item.url));
  expect(retained.some(item => item.url.includes('/webview-cache/retained.css') && item.bytes > 0)).toBe(true);
  const afterEviction = await inspectCache();
  expect(afterEviction).toEqual(expect.arrayContaining(retained));
  expect(afterEviction.some(item => removed.includes(item.url))).toBe(false);
  const beforeEviction = { css: raw(cssUri).length, font: raw(fontUri).length, retained: raw(retainedUri).length };
  await command('markdown.preview.refresh');
  await expect(probe).toHaveCSS('color', 'rgb(61, 72, 83)');
  await expect(probe).toHaveCSS('background-color', 'rgb(90, 91, 92)');
  await probe.evaluate(element => element.ownerDocument.fonts.load('16px CacheProbe').then(() => undefined));
  await expect.poll(() => raw(cssUri).length).toBeGreaterThan(beforeEviction.css);
  await expect.poll(() => raw(fontUri).length).toBeGreaterThan(beforeEviction.font);
  await expect.poll(async () => (await inspectCache()).filter(item => removed.includes(item.url))
    .filter(item => item.etag && item.bytes > 0).length).toBe(2);
  expect(raw(retainedUri)).toHaveLength(beforeEviction.retained);
  expect(await inspectCache()).toEqual(expect.arrayContaining(retained));
  result.eviction = { removed, retained, restored: (await inspectCache()).filter(item => removed.includes(item.url)) };
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error?.stack ?? error);
  if (page) {
    result.currentFrames = page.frames().map(frame => frame.url());
    result.frameDiagnostics = await Promise.allSettled(page.frames().filter(frame =>
      frame.url().includes('.vscode-cdn.net/')).map(async (frame, index) => {
      const path = join(artifacts, `webview-frame-${index}.html`);
      await writeFile(path, await frame.content());
      return { url: frame.url(), path };
    }));
    try { await page.screenshot({ path: join(artifacts, 'webview-cache-failure.png') }); }
    catch (diagnosticError) { failure = new AggregateError([failure, diagnosticError], 'Webview 验证及截图失败'); }
  }
} finally {
  try { await run.close(); }
  catch (cleanupError) { failure = failure ? new AggregateError([failure, cleanupError], 'Webview 验证及清理失败') : cleanupError; }
  await writeFile(required('BOXTEAM_BROWSER_RESULT_PATH'), JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
