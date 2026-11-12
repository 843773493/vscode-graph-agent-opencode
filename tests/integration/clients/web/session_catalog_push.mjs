import { randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!baseUrl || !resultPath) throw new Error('目录推送验收缺少参数');
const scope = TestRunContext.fromTestFile('tests/integration/clients/web/test_session_catalog_push.py');
const result = { phase: 'prepare', pageErrors: [], initialization: [], idleNavigationGets: [], retiredUserRequests: [],
  boundary: '真实 Gateway、托管后端和正式 Workbench；目录操作经产品HTTP入队，无响应替身；断线补齐由观察流和runtime定向测试验证' };
let page;
let failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  scope.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext();
  scope.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  const catalogGets = [];
  const subscriptions = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/gateway/users')) result.retiredUserRequests.push({ method: request.method(), path });
    if (request.method() === 'GET' && /\/session-catalog\/(navigation-events|snapshot)$/.test(path)) catalogGets.push(path);
    if (path === '/api/v1/workspace/observations/stream') subscriptions.push(JSON.parse(request.postData()).subscriptions);
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const cookie = (await context.cookies()).find(item => item.name === 'boxteam_local_token');
  expect(cookie).toBeDefined();
  expect(cookie.httpOnly).toBe(true);
  expect(cookie.sameSite).toBe('Strict');
  expect(cookie.path).toBe('/api/gateway');
  result.localCredentialCookie = { httpOnly: cookie.httpOnly, sameSite: cookie.sameSite, path: cookie.path };
  const credential = await page.evaluate(async () => {
    const response = await fetch('/api/gateway/auth/local-credential');
    if (!response.ok) throw new Error(`本机凭据读取失败：${response.status}`);
    return response.json();
  });
  const headers = { 'X-Local-Token': credential.data.token };
  async function api(path, options = {}) {
    const response = await context.request.fetch(`${baseUrl}${path}`, { ...options, headers });
    const body = await response.json();
    expect(response.ok(), JSON.stringify(body)).toBe(true);
    expect(body.request_id).toBe(response.headers()['x-request-id']);
    return body;
  }
  const health = await api('/api/gateway/health');
  const workspaces = await api('/api/gateway/workspaces');
  const target = workspaces.data.active_workspace;
  expect(target.gateway_id).toBe(health.data.gateway_id);
  headers['X-BoxTeam-Target-Gateway-Id'] = target.gateway_id;
  headers['X-BoxTeam-Workspace-Id'] = target.workspace_id;
  const workspace = await api('/api/v1/workspace');
  expect(workspace.data.workspace_id).toBe(target.workspace_id);
  result.initialization = [health, workspaces, workspace].map(body => body.request_id);
  const workspaceRow = page.getByTestId(`workspace-node-${JSON.stringify([target.gateway_id, target.workspace_id])}`);
  await expect(workspaceRow).toBeVisible();
  const chevron = workspaceRow.locator('.session-resource-chevron');
  if ((await chevron.getAttribute('aria-label')).startsWith('展开')) await chevron.click();
  await expect.poll(() => subscriptions.some(items => items.some(item => item.kind === 'session_navigation'))).toBe(true);
  // 先等待初读完成，再跨越原先每秒轮询的多个周期。
  await page.waitForTimeout(500);
  result.phase = 'idle';
  const idleStart = catalogGets.length;
  await page.waitForTimeout(21_000);
  result.idleNavigationGets = catalogGets.slice(idleStart);
  expect(result.idleNavigationGets).toEqual([]);
  expect(result.retiredUserRequests).toEqual([]);
  result.phase = 'native-image';
  const upload = await context.request.post(`${baseUrl}/api/gateway/ui-assets`, { headers,
    multipart: { file: { name: 'credential-check.png', mimeType: 'image/png',
      buffer: await page.screenshot({ clip: { x: 0, y: 0, width: 2, height: 2 } }) } } });
  expect(upload.status(), await upload.text()).toBe(200);
  const asset = (await upload.json()).data;
  const loaded = await page.evaluate(url => new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image.naturalWidth);
    image.onerror = () => reject(new Error('同源本机凭据未能加载图片'));
    image.src = url;
  }), asset.url);
  expect(loaded).toBe(2);
  const invalid = await context.request.get(`${baseUrl}${asset.url}`, {
    headers: { 'X-Local-Token': 'invalid', 'Sec-Fetch-Site': 'same-origin' } });
  expect(invalid.status()).toBe(401);
  const crossSite = await context.request.get(`${baseUrl}${asset.url}`, { headers: { 'Sec-Fetch-Site': 'cross-site' } });
  expect(crossSite.status()).toBe(403);
  await api(asset.url, { method: 'DELETE' });
  result.nativeImage = { loadedWidth: loaded, invalidCredentialStatus: invalid.status(), crossSiteStatus: crossSite.status() };
  async function createFolder(name) {
    const snapshot = (await api('/api/v1/session-catalog/snapshot')).data;
    const accepted = await api('/api/v1/session-catalog/operations:enqueue', { method: 'POST', data: { intents: [{
      client_operation_id: `op_${randomUUID().replaceAll('-', '')}`, client_sequence: 1,
      kind: 'create_folder', base_catalog_revision: snapshot.catalog_revision, name,
    }] } });
    expect(accepted.data.accepted_count).toBe(1);
  }
  result.phase = 'online-commit';
  await createFolder('在线推送验收目录');
  await expect(page.getByText('在线推送验收目录', { exact: true })).toBeVisible();
  result.onlineUpdateVisible = true;
  result.subscriptions = subscriptions;
  expect(result.retiredUserRequests).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error?.stack ?? error);
} finally {
  const errors = [];
  if (failure && page) {
    try { await page.screenshot({ path: resultPath.replace(/\.json$/, '.png'), fullPage: true }); }
    catch (error) { errors.push(error); }
  }
  try { await writeFile(resultPath, JSON.stringify(result, null, 2)); } catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (failure || errors.length) throw new AggregateError([...(failure ? [failure] : []), ...errors], '目录推送验收失败');
}
