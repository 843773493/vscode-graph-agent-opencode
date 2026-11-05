import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_ARTIFACT_FIXTURE);
assert(fixture.artifacts, '必须显式指定浏览器产物目录');
const scope = TestRunContext.fromTestFile(fixture.testFile);
const result = { classification: 'Integration', boundary: '真实 Workbench/Gateway/Backend/uv；目录及标准 wheel 来自受控本地 PyPI index',
  phase: 'prepare', pageErrors: [], requests: [], responses: [], initialization: [] };
let page;
let failure;
const pending = [];

async function command(id, ...args) {
  await page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  scope.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  scope.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) result.requests.push({ method: request.method(), url: request.url() });
  });
  page.on('response', response => {
    if (/\/mcp\/(installation|candidate)/.test(new URL(response.url()).pathname)) pending.push((async () => {
      const body = await response.json();
      assert.equal(body.request_id, response.headers()['x-request-id']);
      assert.equal(response.status(), 200, JSON.stringify(body));
      result.responses.push({ url: response.url(), method: response.request().method(), status: response.status(), body });
    })());
  });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await page.evaluate(async () => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch('/api/gateway/users/guest', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token },
      body: JSON.stringify({ tracking: { source: 'mcp-artifact-product' } }) });
    if (!response.ok) throw new Error(await response.text());
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  result.phase = 'initialization';
  result.initialization = await page.evaluate(async () => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const observations = [];
    let target;
    for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
      const headers = { 'X-Local-Token': credential.data.token };
      if (path.startsWith('/api/v1/')) {
        headers['X-BoxTeam-Target-Gateway-Id'] = target.gateway_id;
        headers['X-BoxTeam-Workspace-Id'] = target.workspace_id;
      }
      const response = await fetch(path, { headers });
      const body = await response.json();
      if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, body }));
      if (path.endsWith('/workspaces')) target = body.data.active_workspace;
      if (path === '/api/v1/workspace' && body.data.workspace_id !== target.workspace_id) throw new Error('初始化工作区身份不一致');
      observations.push({ path, requestId: body.request_id, target });
    }
    return observations;
  });
  result.phase = 'gallery-open';
  await command('workbench.mcp.browseServers');
  await expect(page.locator('.part.sidebar .message-container .message').filter({ visible: true })).toContainText('MCP gallery is not configured');
  result.phase = 'gallery-settings';
  await command('workbench.action.openSettingsJson');
  const editor = page.locator('.monaco-editor').filter({ visible: true }).last();
  await expect(editor.locator('.view-lines')).toBeVisible();
  await editor.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText(JSON.stringify({ 'chat.mcp.gallery.serviceUrl': fixture.registryUrl }, null, 2) + '\n');
  await command('workbench.action.files.save');
  const settingsDialog = page.getByRole('dialog', { name: /settings\.json$/ });
  if (await settingsDialog.isVisible()) await page.keyboard.press('Escape');
  else await command('workbench.action.closeActiveEditor');
  result.phase = 'gallery-details';
  const fetched = page.waitForResponse(response => response.url().startsWith(fixture.registryUrl)
    && new URL(response.url()).searchParams.get('search') === 'artifact');
  const [response] = await Promise.all([fetched, command('workbench.extensions.search', '@mcp artifact')]);
  assert.equal(response.status(), 200);
  const responseFailure = await response.finished();
  if (responseFailure) throw responseFailure;
  const list = page.locator('.part.sidebar .mcp-servers-list').filter({ visible: true });
  await expect(list.getByText('Artifact Server', { exact: true })).toBeVisible();
  await list.getByText('Artifact Server', { exact: true }).click();
  const detail = page.locator('.mcp-server-editor').filter({ has: page.getByRole('heading', { name: 'Artifact Server', exact: true }) }).filter({ visible: true });
  const install = detail.getByRole('button', { name: 'Install', exact: true });
  await expect(install).toBeEnabled();
  result.phase = 'explicit-variant';
  await install.click();
  const quick = page.locator('.quick-input-widget');
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: 'pypi: artifact-server 1.0.0' }).click();
  result.phase = 'explicit-entrypoint';
  await expect(quick).toContainText('输入根 distribution 的 console entry point');
  await quick.locator('input').first().fill('artifact-server');
  await page.keyboard.press('Enter');
  result.phase = 'published';
  await expect(page.locator('.notifications-toasts')).toContainText('工作区尚未配置或连接', { timeout: 40_000 });
  await Promise.all(pending);
  const submitted = result.responses.filter(item => item.method === 'POST' && new URL(item.url).pathname.endsWith('/mcp/installations'));
  assert.equal(submitted.length, 1, '原安装意图只能提交一次');
  const complete = result.responses.find(item => item.body.data?.status === 'succeeded');
  assert(complete?.body.data.artifact, '原安装操作必须真实确认发布');
  result.operation = complete.body.data;
  assert.equal(result.operation.artifact.identifier, 'artifact-server');
  assert.equal(result.operation.artifact.entrypoint, 'artifact-server');
  assert(result.responses.some(item => item.url.endsWith('/mcp/candidate-manifests')));
  assert(result.responses.some(item => item.url.endsWith('/mcp/candidate-plans')));
  assert(!result.requests.some(item => item.method !== 'GET' && /\/config\/sources|\/mcp\/connection-operations/.test(item.url)), '发布不能暗写工作区配置或启动连接');
  await expect(install).toBeEnabled();
  await expect(detail.getByRole('button', { name: /^(Start Server|Stop Server|Authorize)$/ })).toHaveCount(0);
  assert.deepEqual(result.pageErrors, []);
  await page.screenshot({ path: join(fixture.artifacts, 'installation-published.png'), fullPage: true });
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error?.stack ?? error);
} finally {
  if (failure && page) {
    try { await page.screenshot({ path: join(fixture.artifacts, 'installation-failure.png'), fullPage: true }); }
    catch (error) { failure = new AggregateError([failure, error], '安装场景与截图均失败'); }
  }
  try { await Promise.all(pending); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '安装场景与响应核验均失败') : error; }
  try { await writeFile(join(fixture.artifacts, 'browser-installation-result.json'), JSON.stringify(result, null, 2)); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '安装场景与证据保存均失败') : error; }
  try { await scope.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '安装场景与资源回收均失败') : error; }
}
if (failure) throw failure;
