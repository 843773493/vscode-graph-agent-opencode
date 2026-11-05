import assert from 'node:assert/strict';
import { appendFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_GALLERY_FIXTURE);
assert(fixture.artifacts, '必须显式指定正式浏览器产物目录');
const scope = TestRunContext.fromTestFile('tests/integration/clients/web/test_mcp_gallery.py');
const result = { classification: 'Integration', boundary: '完整 Workbench、真实 Gateway/Backend；仅 MCP registry 是明确受控 HTTP 服务，无 MCP 安装或启动',
  phase: 'prepare', pageErrors: [], consoleErrors: [], initialization: [], registryRequests: [], mutations: [], maintenanceRequests: [], cancelled: [], listNavigation: [] };
let page;
let failure;

async function step(event, details = {}) {
  await appendFile(join(fixture.artifacts, 'gallery-driver-steps.ndjson'), JSON.stringify({
    at: new Date().toISOString(), phase: result.phase, event, ...details,
  }) + '\n');
}
async function command(id, ...args) {
  await step('command-start', { id });
  let timer;
  try {
    await Promise.race([
      // 驱动只等待命令完成；编辑器命令返回的原 IEditorPane 不应跨 CDP 序列化。
      page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args }),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`原命令 30 秒内未返回：${id}`)), 30_000); }),
    ]);
    await step('command-complete', { id });
  } catch (error) {
    await step('command-failed', { id, error: String(error) });
    throw error;
  } finally { clearTimeout(timer); }
}
async function configure(source, version) {
  await step('settings-open', { source, version });
  await command('workbench.action.openSettingsJson');
  const editor = page.locator('.monaco-editor').filter({ visible: true }).last();
  await expect(editor.locator('.view-lines')).toBeVisible();
  await step('settings-input', { source, version });
  await editor.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  const settings = { 'chat.mcp.gallery.serviceUrl': source, ...(version ? { 'chat.mcp.gallery.version': version } : {}) };
  await page.keyboard.insertText(JSON.stringify(settings, null, 2) + '\n');
  await expect(editor.locator('.view-lines')).toContainText('chat.mcp.gallery.serviceUrl');
  await command('workbench.action.files.save');
  const dialog = page.getByRole('dialog', { name: /settings\.json$/ });
  if (await dialog.isVisible()) await page.keyboard.press('Escape');
  else await command('workbench.action.closeActiveEditor');
  await step('settings-closed', { source, version });
}
async function search(text) {
  const response = page.waitForResponse(response => response.url().startsWith(fixture.registryUrl)
    && new URL(response.url()).searchParams.get('search') === text);
  const [fetched] = await Promise.all([response, command('workbench.extensions.search', `@mcp ${text}`)]);
  const failure = await fetched.finished();
  if (failure) throw failure;
  return fetched;
}
function list() { return page.locator('.part.sidebar .mcp-servers-list').filter({ visible: true }); }
function message() { return page.locator('.part.sidebar .message-container .message').filter({ visible: true }); }
async function navigateList(label, key) {
  const widget = list().getByRole('list', { name: 'MCP Servers', exact: true });
  await widget.focus();
  await expect(widget).toBeFocused();
  const snapshot = async stage => result.listNavigation.push({ label, stage, ...await widget.evaluate(element => {
    const scrollable = element.querySelector('.monaco-scrollable-element');
    const rows = element.querySelector('.monaco-list-rows');
    if (!scrollable || !rows) throw new Error('原列表缺少滚动容器');
    return {
      // 原虚拟列表用 rows 的 top 表示滚动位置，原生容器的 scrollTop 始终归零。
      scrollTop: -Number.parseFloat(rows.style.top || '0'), scrollHeight: rows.clientHeight, clientHeight: element.clientHeight,
      nativeScrollTop: scrollable.scrollTop,
      renderedTop: rows.style.top, renderedHeight: rows.clientHeight,
      rowIndices: [...rows.querySelectorAll('.monaco-list-row')].map(row => Number(row.getAttribute('data-index'))),
    };
  }) });
  await snapshot('before');
  // 使用原 Workbench 的 Home/End 键绑定导航，不直接修改虚拟列表滚动状态。
  await page.keyboard.press(key);
  await snapshot('after');
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  scope.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  scope.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', entry => { if (entry.type() === 'error') result.consoleErrors.push(entry.text()); });
  page.on('request', request => {
    if (request.url().startsWith(fixture.registryUrl)) result.registryRequests.push(request.url());
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      const record = { url: request.url(), method: request.method() };
      // 这两个既有 POST 分别续期页面身份与订阅只读观察流，完整保留，但不计为目录写操作。
      if (request.method() === 'POST' && ['/api/gateway/users/current/heartbeat', '/api/v1/workspace/observations/stream'].includes(path)) result.maintenanceRequests.push(record);
      else result.mutations.push(record);
    }
  });
  page.on('requestfailed', request => { if (request.url().startsWith(fixture.registryUrl)) result.cancelled.push(request.url()); });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await page.evaluate(async () => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch('/api/gateway/users/guest', { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-Local-Token': credential.data.token,
    }, body: JSON.stringify({ tracking: { source: 'mcp-gallery-product' } }) });
    if (!response.ok) throw new Error(await response.text());
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  result.phase = 'gateway-initialization';
  result.initialization = await page.evaluate(async () => {
    const observations = [];
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    let owner;
    for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
      const headers = { 'X-Local-Token': credential.data.token };
      if (path.startsWith('/api/v1/')) {
        headers['X-BoxTeam-Target-Gateway-Id'] = owner.gateway_id;
        headers['X-BoxTeam-Workspace-Id'] = owner.workspace_id;
      }
      const response = await fetch(path, { headers });
      const body = await response.json();
      if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
      if (path.endsWith('/workspaces')) owner = body.data.active_workspace;
      if (path === '/api/v1/workspace' && body.data.workspace_id !== owner.workspace_id) throw new Error('工作区身份不一致');
      observations.push({ path, requestId: body.request_id, owner });
    }
    return observations;
  });

  result.phase = 'unconfigured';
  await command('workbench.mcp.browseServers');
  await expect(message()).toContainText('MCP gallery is not configured');
  await expect(page.locator('#startup-error')).toBeEmpty();
  result.unconfigured = true;
  assert.equal(result.registryRequests.length, 0);
  result.mutations = [];

  result.phase = 'configure-and-query';
  await configure(`${fixture.registryUrl}/alpha`);
  const firstResponse = await search('pages');
  await expect(list().getByText('Gallery Alpha 0', { exact: true })).toBeVisible();
  await expect(list().locator('.description').first()).toHaveText('Alpha selected registry entry · pages');
  const firstUrl = new URL(firstResponse.url());
  assert.equal(firstUrl.pathname, '/alpha/v0.1/servers');
  assert.equal(firstUrl.searchParams.get('limit'), '50');
  const continuation = page.waitForResponse(response => response.url().startsWith(fixture.registryUrl)
    && new URL(response.url()).searchParams.get('cursor') === 'opaque+/=');
  await navigateList('first-page-sentinel', 'End');
  await (await continuation).finished();
  await expect(list().getByText('Gallery Alpha 50', { exact: true })).toBeVisible();
  await navigateList('last-page', 'End');
  await expect(list().getByText('Gallery Alpha 52', { exact: true })).toBeVisible();
  assert(result.registryRequests.some(url => new URL(url).searchParams.get('cursor') === 'opaque+/=' && !new URL(url).searchParams.has('search')));
  result.paginated = true;

  result.phase = 'pagination-http-error';
  const failedContinuation = page.waitForResponse(response => response.url().startsWith(fixture.registryUrl)
    && new URL(response.url()).searchParams.get('cursor') === 'failed+/=');
  await search('badpage');
  await expect(list().locator('.description').first()).toHaveText('Alpha selected registry entry · badpage');
  await navigateList('failed-page-sentinel', 'End');
  assert.equal((await failedContinuation).status(), 503);
  await expect(message()).toContainText('HTTP 503');
  await expect(page.locator('#startup-error')).toBeEmpty();
  assert(result.registryRequests.some(url => new URL(url).searchParams.get('cursor') === 'failed+/=' && !new URL(url).searchParams.has('search')));
  result.paginationFailure = true;

  result.phase = 'read-only-original-details';
  await search('details');
  await expect(list().locator('.description').first()).toHaveText('Alpha selected registry entry · details');
  await navigateList('details-first-entry', 'Home');
  await list().getByText('Gallery Alpha 0', { exact: true }).click();
  const detail = page.locator('.mcp-server-editor').filter({ has: page.getByRole('heading', { name: 'Gallery Alpha 0', exact: true }) }).filter({ visible: true });
  await expect(detail).toBeVisible();
  await expect(detail).toContainText(`${fixture.registryUrl}/alpha`);
  await expect(detail.getByRole('button', { name: /^(Start Server|Stop Server|Authorize)$/ })).toHaveCount(0);
  await expect(detail.getByRole('button', { name: 'Install', exact: true })).toBeDisabled();
  const writeRefusal = await page.evaluate(async () => {
    try { await globalThis.workbench.commands.executeCommand('workbench.mcp.addConfiguration'); return null; }
    catch (error) { return String(error); }
  });
  assert.match(writeRefusal, /工作区配置与凭据写入.*尚未接入/);
  result.readOnlyDetails = true;

  result.phase = 'empty-and-http-error';
  await search('nomatch');
  await expect(message()).toHaveText('No MCP Servers found.');
  result.noMatch = true;
  await search('fail');
  await expect(message()).toContainText('HTTP 503');
  await expect(page.locator('#startup-error')).toBeEmpty();
  result.httpFailure = true;

  result.phase = 'source-switch-cancels-old-query';
  const slowRequest = page.waitForRequest(request => request.url().includes('/alpha/v0.1/servers') && new URL(request.url()).searchParams.get('search') === 'slow');
  const oldSearch = command('workbench.extensions.search', '@mcp slow').catch(error => String(error));
  await slowRequest;
  await step('slow-request-observed');
  await configure(`${fixture.registryUrl}/beta`);
  await search('fresh');
  await expect(list().getByText('Gallery Beta 0', { exact: true })).toBeVisible();
  await page.evaluate(async registryUrl => { await fetch(`${registryUrl}/release`, { method: 'POST' }); }, fixture.registryUrl);
  await oldSearch;
  await expect.poll(() => result.cancelled.some(url => url.includes('/alpha/') && url.includes('search=slow'))).toBe(true);
  await expect(list().getByText('Gallery Alpha 0', { exact: true })).toHaveCount(0);
  await expect(list().getByText('Gallery Beta 0', { exact: true })).toBeVisible();
  result.cancelledOnSourceChange = true;

  result.phase = 'explicit-v0';
  await configure(`${fixture.registryUrl}/legacy`);
  await search('legacy-default');
  await expect(message()).toContainText('HTTP 404');
  assert(result.registryRequests.some(url => new URL(url).pathname === '/legacy/v0.1/servers'));
  assert(!result.registryRequests.some(url => new URL(url).pathname === '/legacy/v0/servers'));
  result.v0RequiresExplicitVersion = true;
  await configure(`${fixture.registryUrl}/legacy`, 'v0');
  await search('legacy-explicit');
  await expect(list()).toContainText('Legacy directory entry');
  assert(result.registryRequests.some(url => new URL(url).pathname === '/legacy/v0/servers'));
  result.explicitV0 = true;
  assert(result.registryRequests.every(url => !new URL(url).searchParams.has('limit') || new URL(url).searchParams.get('limit') !== '1'));
  assert.equal(result.mutations.length, 0, JSON.stringify(result.mutations));
  assert.deepEqual(result.pageErrors, []);
  await expect(page.locator('#startup-error')).toBeEmpty();
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error?.stack ?? error);
} finally {
  if (failure && page) {
    try { await page.screenshot({ path: join(fixture.artifacts, 'gallery-failure.png'), fullPage: true }); }
    catch (error) { result.screenshotError = String(error); failure = new AggregateError([failure, error], '场景与截图均失败'); }
  }
  try { await writeFile(join(fixture.artifacts, 'gallery-result.json'), JSON.stringify(result, null, 2)); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '场景与证据写入均失败') : error; }
  try { await scope.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '场景与资源回收均失败') : error; }
}
if (failure) throw failure;
