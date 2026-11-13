// Integration：完整源码副本、产品 Vite 入口和真实 Gateway；只改副本的观测属性与翻译。
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
};
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_development_hmr.py');
const result = { classification: 'Integration', phase: 'prepare', routeStubs: [],
  boundary: '独占完整源码副本的原 React 导航观测属性及 NLS 补译；产品 Vite／Gateway；不代替发行或 Electron',
  pageErrors: [], consoleErrors: [], failedResponses: [], websocket: [], initialization: [], changes: [], scripts: [], moduleUrls: [], browserEvents: [] };
const nlsResponses = [];
const observationStartedAt = performance.now();
const recordBrowserEvent = event => result.browserEvents.push({
  at: new Date().toISOString(), elapsed_ms: Math.round(performance.now() - observationStartedAt), ...event,
});
const requestStarts = new Map(), observedPages = new Set();
const requestCounts = { issued: {}, settled: {}, failed: {}, longest_ms: {} }, requestFailures = [];
const requestPath = request => new URL(request.url()).pathname;
const requestCategory = path => path.includes('/node_modules/') || path.startsWith('/node_modules/') ? 'node_modules'
  : path.startsWith('/out/dev-upstream/') ? 'upstream-bundle'
  : path.startsWith('/upstream/') ? 'upstream'
  : path.startsWith('/src/') || path.startsWith('/@fs/') ? 'project-source'
  : path.startsWith('/api/') ? 'api' : 'other';
const count = (counts, category) => { counts[category] = (counts[category] ?? 0) + 1; };
const recordDuration = (category, elapsed) => {
  requestCounts.longest_ms[category] = Math.max(requestCounts.longest_ms[category] ?? 0, elapsed);
};
const summarizeRequests = () => {
  const active = [...requestStarts.values()].map(started => ({
    path: started.path, category: started.category, method: started.method, type: started.type,
    elapsed_ms: Math.round(performance.now() - started.startedAt),
  }));
  return { ...requestCounts, requestFailures,
    inFlight: Object.fromEntries([...new Set(active.map(item => item.category))]
      .map(category => [category, active.filter(item => item.category === category).length])),
    longestInFlight: active.sort((left, right) => right.elapsed_ms - left.elapsed_ms).slice(0, 20),
  };
};
const trackPage = observed => {
  observedPages.add(observed);
  observed.on('close', () => recordBrowserEvent({ type: 'page-close', url: observed.url() }));
  observed.on('crash', error => recordBrowserEvent({ type: 'page-crash', error: String(error) }));
  observed.on('framenavigated', frame => {
    if (frame === observed.mainFrame()) recordBrowserEvent({ type: 'main-frame-navigation', url: frame.url() });
  });
  observed.on('request', request => {
    const startedAt = performance.now();
    const path = requestPath(request);
    const category = requestCategory(path);
    requestStarts.set(request, { startedAt, path, category, method: request.method(), type: request.resourceType() });
    count(requestCounts.issued, category);
  });
  observed.on('requestfinished', async request => {
    const started = requestStarts.get(request);
    if (!started) return;
    requestStarts.delete(request);
    const response = await request.response();
    const elapsed = Math.round(performance.now() - started.startedAt);
    count(requestCounts.settled, started.category);
    recordDuration(started.category, elapsed);
    if (response && response.status() >= 400) requestFailures.push({ path: started.path, category: started.category, status: response.status() });
  });
  observed.on('requestfailed', request => {
    const started = requestStarts.get(request);
    if (!started) return;
    requestStarts.delete(request);
    const elapsed = Math.round(performance.now() - started.startedAt);
    count(requestCounts.failed, started.category);
    recordDuration(started.category, elapsed);
    requestFailures.push({ path: started.path, category: started.category, elapsed_ms: elapsed, error: request.failure()?.errorText });
  });
};
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  browser.on('disconnected', () => recordBrowserEvent({ type: 'browser-disconnected' }));
  recordBrowserEvent({ type: 'browser-connected' });
  run.addCleanup('hmr-browser', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('hmr-context', () => context.close());
  context.on('page', observed => {
    trackPage(observed);
    observed.on('pageerror', error => result.pageErrors.push(String(error)));
    observed.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
    observed.on('response', response => {
      if (response.status() >= 400) result.failedResponses.push({ url: response.url(), status: response.status() });
      const path = new URL(response.url()).pathname;
      if (/^\/out\/nls-(?:assets-[a-f0-9]+|zh-cn-[a-f0-9]+)\.json$/.test(path)) {
        nlsResponses.push(response.json().then(body => ({ path, body }), error => ({ path, error: String(error) })));
      }
    });
    observed.on('websocket', socket => socket.on('framereceived', ({ payload }) => {
      if (typeof payload === 'string') result.websocket.push(JSON.parse(payload));
    }));
  });
  page = await context.newPage();
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toHaveAttribute('data-hmr-acceptance', 'version-1', { timeout: 45000 });
  result.initialization = await page.evaluate(async () => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const headers = { 'X-Local-Token': credential.data.token };
    const read = async path => {
      const response = await fetch(path, { headers });
      const payload = await response.json();
      if (!response.ok || !payload.request_id || payload.request_id !== response.headers.get('X-Request-ID')) {
        throw new Error(JSON.stringify({ path, status: response.status, payload }));
      }
      return { path, request_id: payload.request_id, data: payload.data };
    };
    const health = await read('/api/gateway/health'), workspaces = await read('/api/gateway/workspaces');
    const owner = workspaces.data.active_workspace;
    headers['X-BoxTeam-Target-Gateway-Id'] = owner.gateway_id;
    headers['X-BoxTeam-Workspace-Id'] = owner.workspace_id;
    const workspace = await read('/api/v1/workspace');
    if (health.data.gateway_id !== owner.gateway_id || workspace.data.workspace_id !== owner.workspace_id) {
      throw new Error('Vite 初始化的稳定目标不一致');
    }
    return [health, workspaces, workspace];
  });
  // health 已等待入口与 optimizer 完成；保留唯一真实页面，避免双页同时全量重载。
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('boxteam.flushViewState'));
  result.lifecycleCommand = 'boxteam.flushViewState';
  await page.evaluate(() => { void globalThis.workbench.commands.executeCommand('workbench.action.openSettings'); });
  await expect(page.locator('.settings-editor')).toBeVisible({ timeout: 30000 });
  result.settingsModuleUrls = await page.evaluate(() => performance.getEntriesByType('resource')
    .map(entry => new URL(entry.name).pathname).filter(path => path.includes('/shared/core/i18n/index.ts')));
  expect(result.settingsModuleUrls).toEqual([required('BOXTEAM_HMR_SHARED_I18N_URL')]);
  result.settingsIdentity = await page.evaluate(async sourceUrl => {
    const module = await import(sourceUrl);
    return { locale: module.getLocale(), message: module.t('workbench.settings.editorName', 'Identity') };
  }, result.settingsModuleUrls[0]);
  expect(result.settingsIdentity).toEqual({ locale: 'zh-cn', message: '设置 · Identity' });
  await page.evaluate(() => { void globalThis.workbench.commands.executeCommand('workbench.action.closeActiveEditor'); });
  await expect(page.locator('.settings-editor')).toBeHidden({ timeout: 30000 });
  result.moduleUrls = await page.evaluate(() => [...new Set(performance.getEntriesByType('resource')
    .map(entry => new URL(entry.name).pathname)
    .filter(path => path.startsWith('/out/dev-upstream/') || path.startsWith('/upstream/src/vs/')
      || path.startsWith('/@fs/')))].sort());
  result.phase = 'source-update';
  const source = required('BOXTEAM_HMR_SOURCE'), before = await readFile(source, 'utf8');
  expect(before.split('data-hmr-acceptance="version-1"')).toHaveLength(2);
  const after = before.replace('data-hmr-acceptance="version-1"', 'data-hmr-acceptance="version-2"');
  result.changes.push({ source, before: createHash('sha256').update(before).digest('hex'),
    after: createHash('sha256').update(after).digest('hex') });
  await writeFile(source, after);
  // 不调用 reload，不重建服务，不拦截或替换响应；由真实 Vite 通知并更新页面。
  await expect(page.locator('.native-session-navigation')).toHaveAttribute('data-hmr-acceptance', 'version-2', { timeout: 30000 });
  expect(result.websocket.some(event => event.type === 'update' || event.type === 'full-reload')).toBe(true);
  result.scripts = await page.locator('script[src]').evaluateAll(scripts => scripts.map(script => script.src));
  const scriptPaths = result.scripts.map(source => new URL(source).pathname);
  expect(scriptPaths.filter(path => path === '/out/main.js')).toHaveLength(0);
  expect(scriptPaths.filter(path => path === '/@vite/client')).toHaveLength(1);
  expect(scriptPaths.filter(path => path === '/src/bootstrap.ts')).toHaveLength(1);
  result.phase = 'translation-update';
  const command = id => page.evaluate(id => globalThis.workbench.commands.executeCommand(id), id);
  const quick = page.locator('.quick-input-widget');
  const owner = result.initialization.find(item => item.path === '/api/gateway/workspaces').data.active_workspace;
  const previewUri = `boxteam://${owner.gateway_id}/workspace/${owner.workspace_id}/resources/files/root/development-locale.md`;
  await page.evaluate(uri => globalThis.workbench.commands.executeCommand('workbench.action.quickOpen', uri), previewUri);
  await quick.locator(`[data-quick-input-id="link:${previewUri}"]`).click();
  await command('markdown.showPreview');
  const preview = page.frameLocator('iframe.webview:visible').frameLocator('iframe#active-frame');
  await expect(preview.locator('h1')).toHaveText('Development locale preview');
  await expect(preview.locator('#code-csp-warning')).toHaveText('已禁用此文档中的部分内容');
  await expect(preview.locator('#code-csp-warning')).toHaveAttribute('aria-label', '已禁用内容安全警告');
  result.developmentPreviewLocalized = true;
  await command('workbench.action.closeActiveEditor');
  await command('workbench.action.configureLocale');
  await expect(quick).toBeVisible();
  const originalPlaceholder = await quick.locator('input').first().getAttribute('placeholder');
  await page.keyboard.press('Escape');
  const translations = required('BOXTEAM_HMR_TRANSLATIONS');
  const translationBefore = await readFile(translations, 'utf8');
  const messages = JSON.parse(translationBefore);
  const updatedPlaceholder = '选择显示语言 · 开发更新验收';
  expect(originalPlaceholder).not.toBe(updatedPlaceholder);
  messages.contents['vs/workbench/contrib/localization/common/localizationsActions'].chooseLocale = updatedPlaceholder;
  const translationAfter = JSON.stringify(messages, null, 2) + '\n';
  result.changes.push({ source: translations, before: createHash('sha256').update(translationBefore).digest('hex'),
    after: createHash('sha256').update(translationAfter).digest('hex') });
  // 等待页面 bootstrap 自己请求的新 manifest 与语言资源，不额外发探测请求。
  const previousManifestUrl = await page.evaluate(() => performance.getEntriesByType('resource')
    .map(entry => new URL(entry.name).pathname).findLast(path => /^\/out\/nls-assets-[a-f0-9]+\.json$/.test(path)));
  expect(previousManifestUrl).toMatch(/^\/out\/nls-assets-[a-f0-9]+\.json$/);
  const previousNlsResponseCount = nlsResponses.length;
  await writeFile(translations, translationAfter);
  let updatedNls;
  await expect.poll(async () => {
    const responses = await Promise.all(nlsResponses.slice(previousNlsResponseCount));
    const failedResponse = responses.find(entry => entry.error);
    if (failedResponse) throw new Error(`读取实际 NLS 响应失败 ${failedResponse.path}: ${failedResponse.error}`);
    const manifestEntry = responses.find(({ path }) => /^\/out\/nls-assets-[a-f0-9]+\.json$/.test(path)
      && path !== previousManifestUrl);
    if (!manifestEntry) return null;
    const manifest = manifestEntry.body;
    const localized = responses.find(({ path }) => path === manifest.assets?.['zh-cn'])?.body;
    if (!localized) return null;
    updatedNls = { manifest, localized };
    return updatedNls;
  }, { timeout: 120000, intervals: [250, 500, 1000] }).toMatchObject({
    manifest: { buildId: expect.any(String) },
    localized: { buildId: expect.any(String), contents: { 'vs/workbench/contrib/localization/common/localizationsActions': { chooseLocale: updatedPlaceholder } } },
  });
  expect(updatedNls.manifest.buildId).not.toBe(previousManifestUrl.match(/nls-assets-([a-f0-9]+)\.json$/)?.[1]);
  expect(updatedNls.localized.buildId).toBe(updatedNls.manifest.buildId);
  await expect(page.locator('.native-session-navigation')).toHaveAttribute('data-hmr-acceptance', 'version-2', { timeout: 45000 });
  await command('workbench.action.configureLocale');
  await expect(quick.locator('input').first()).toHaveAttribute('placeholder', updatedPlaceholder);
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
  result.translationUpdate = { originalPlaceholder, updatedPlaceholder };
  await page.keyboard.press('Escape');
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error?.stack ?? error);
  result.requestMetrics = summarizeRequests();
  result.pages = await Promise.all([...observedPages].map(async observed => ({
    path: new URL(observed.url()).pathname,
    title: await observed.title().catch(() => ''),
    readyState: await observed.evaluate(() => document.readyState).catch(() => 'unavailable'),
  })));
  if (page) {
    try { await page.screenshot({ path: join(run.artifactsDir, 'development-hmr-failure.png') }); }
    catch (diagnosticError) { failure = new AggregateError([failure, diagnosticError], 'HMR 验证及截图均失败'); }
  }
} finally {
  result.requestMetrics ??= summarizeRequests();
  try { await run.close(); }
  catch (cleanupError) { failure = failure ? new AggregateError([failure, cleanupError], 'HMR 验证及清理均失败') : cleanupError; }
  await writeFile(required('BOXTEAM_BROWSER_RESULT_PATH'), JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
