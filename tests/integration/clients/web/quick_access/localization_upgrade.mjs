// Integration：两份真实正式构建、同来源/profile 和原 HTTP 缓存，不改写产物或响应。
import { writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`本地化升级验收缺少 ${name}`);
  return value;
};
const base = required('BOXTEAM_BROWSER_BASE_URL'), output = required('BOXTEAM_BROWSER_RESULT_PATH');
const expected = JSON.parse(required('BOXTEAM_LOCALIZATION_EXPECTED'));
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_static_upgrade.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], network: [],
  externalTranslationRequests: [],
  boundary: 'A/B 均由完整源码的正式 build 生成，仅两份独占翻译输入不同；Gateway 同端口正常重启，Chromium 同 profile 正常关闭重开，不预写缓存或替换响应。' };
let page, failure;
try {
  const context = await chromium.launchPersistentContext(join(required('BOXTEAM_BROWSER_RUNTIME_PATH'), 'localization-profile'), {
    executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), locale: 'en-US', headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  run.addCleanup('localization-browser', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push({ phase: result.phase, message: String(error), stack: error.stack }));
  page.on('request', request => {
    const url = new URL(request.url());
    if (/^https?:$/.test(url.protocol) && url.origin !== new URL(base).origin
      && /locale|nls|l10n|i18n|gallery|marketplace|vsix/i.test(url.href)) result.externalTranslationRequests.push(url.href);
  });
  const cdp = await context.newCDPSession(page), records = new Map();
  const record = id => {
    if (!records.has(id)) records.set(id, { id, bytes: 0 });
    return records.get(id);
  };
  const headers = values => Object.fromEntries(Object.entries(values)
    .filter(([key]) => ['if-none-match', 'etag', 'cache-control'].includes(key.toLowerCase()))
    .map(([key, value]) => [key.toLowerCase(), value]));
  cdp.on('Network.requestWillBeSent', event => {
    const path = new URL(event.request.url).pathname;
    if (path !== '/out/main.js' && !path.startsWith('/out/nls-')) return;
    const item = record(event.requestId);
    Object.assign(item, { path, phase: result.phase }); result.network.push(item);
  });
  cdp.on('Network.requestWillBeSentExtraInfo', event => { record(event.requestId).request = headers(event.headers); });
  cdp.on('Network.responseReceivedExtraInfo', event => {
    Object.assign(record(event.requestId), { status: event.statusCode, response: headers(event.headers) });
  });
  cdp.on('Network.responseReceived', event => {
    const item = record(event.requestId);
    item.response ??= headers(event.response.headers); item.status ??= event.response.status;
    item.fromDiskCache = event.response.fromDiskCache;
  });
  cdp.on('Network.requestServedFromCache', event => { record(event.requestId).servedFromCache = true; });
  cdp.on('Network.dataReceived', event => { record(event.requestId).bytes += event.encodedDataLength; });
  cdp.on('Network.loadingFinished', event => { record(event.requestId).finished = true; });
  await cdp.send('Network.enable');
  const command = id => page.evaluate(id => globalThis.workbench.commands.executeCommand(id), id);
  const ready = async () => {
    await expect(page.getByRole('button', { name: expected.product, exact: true })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
    await command('workbench.action.configureLocale');
    const quick = page.locator('.quick-input-widget');
    await expect(quick.locator('input').first()).toHaveAttribute('placeholder', expected.placeholder);
    await page.keyboard.press('Escape');
  };
  result.phase = 'cold';
  await page.goto(base, { waitUntil: 'domcontentloaded' }); await ready();
  const coldMain = result.network.find(item => item.phase === 'cold' && item.path === '/out/main.js');
  await expect.poll(() => coldMain?.finished).toBe(true);
  expect(coldMain.status).toBe(200); expect(coldMain.bytes).toBeGreaterThan(0);
  result.coldMain = coldMain;
  result.sourceLanguage = expected.language;
  result.initialization = await page.evaluate(async () => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const receipts = [];
    const headers = { 'X-Local-Token': token };
    const read = async path => {
      const response = await fetch(path, { headers });
      const payload = await response.json(), requestId = response.headers.get('X-Request-ID');
      if (!response.ok || !requestId || payload.request_id !== requestId) throw new Error(`初始化失败：${path}`);
      receipts.push({ path, requestId }); return payload.data;
    };
    const health = await read('/api/gateway/health'), listing = await read('/api/gateway/workspaces');
    const owner = listing.active_workspace;
    if (health.gateway_id !== owner.gateway_id) throw new Error('Gateway 身份不一致');
    headers['X-BoxTeam-Target-Gateway-Id'] = owner.gateway_id;
    headers['X-BoxTeam-Workspace-Id'] = owner.workspace_id;
    const workspace = await read('/api/v1/workspace');
    if (workspace.workspace_id !== owner.workspace_id) throw new Error('Workspace 身份不一致');
    return { receipts, owner: { gatewayId: owner.gateway_id, workspaceId: owner.workspace_id } };
  });
  result.owner = result.initialization.owner;
  const manifest = result.network.find(item => item.phase === 'cold' && item.path === expected.manifest);
  const language = result.network.find(item => item.phase === 'cold' && item.path === expected.language);
  expect(manifest).toBeTruthy(); expect(language).toBeTruthy();
  expect(result.network.filter(item => item.phase === 'cold' && item.path.startsWith('/out/nls-assets-'))).toHaveLength(1);
  expect(result.network.filter(item => item.phase === 'cold' && item.path.startsWith('/out/nls-zh-cn-'))).toHaveLength(1);
  result.phase = 'warm';
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  const warmMain = result.network.find(item => item.phase === 'warm' && item.path === '/out/main.js');
  await expect.poll(() => warmMain?.finished).toBe(true);
  expect(warmMain.status).toBe(304); expect(warmMain.bytes).toBe(0);
  expect(warmMain.request['if-none-match']).toBe(coldMain.response.etag);
  const warmLanguage = result.network.find(item => item.phase === 'warm' && item.path === expected.language);
  await expect.poll(() => warmLanguage?.finished).toBe(true);
  expect(warmLanguage.fromDiskCache || warmLanguage.servedFromCache).toBe(true);
  expect(warmLanguage.bytes).toBe(0);
  expect(warmLanguage.response['cache-control']).toBe('public, max-age=31536000, immutable');
  expect(result.externalTranslationRequests).toEqual([]); expect(result.pageErrors).toEqual([]);
  result.phase = 'passed';
} catch (error) {
  failure = error; result.error = String(error?.stack ?? error);
  if (page) {
    try { await page.screenshot({ path: join(dirname(output), 'localization-upgrade-failure.png') }); }
    catch (captureError) { failure = new AggregateError([failure, captureError], '升级验收和截图失败'); }
  }
} finally {
  try { await run.close(); }
  catch (closeError) { failure = failure ? new AggregateError([failure, closeError], '升级验收和清理失败') : closeError; }
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (saveError) { failure = failure ? new AggregateError([failure, saveError], '升级验收和原件保存失败') : saveError; }
  if (failure) throw failure;
}
