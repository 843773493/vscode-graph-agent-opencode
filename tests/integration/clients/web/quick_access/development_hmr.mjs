// Integration：完整源码副本、产品 Vite 入口和真实 Gateway；只改原导航的观测属性。
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
  boundary: '独占完整源码副本的原 React 导航观测属性；产品 Vite／Gateway；不代替发行或 Electron',
  pageErrors: [], consoleErrors: [], failedResponses: [], websocket: [], initialization: [], changes: [], scripts: [] };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('hmr-browser', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('hmr-context', () => context.close());
  context.on('page', observed => {
    observed.on('pageerror', error => result.pageErrors.push(String(error)));
    observed.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
    observed.on('response', response => {
      if (response.status() >= 400) result.failedResponses.push({ url: response.url(), status: response.status() });
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
  // 预构建完成后，新页面仍须正确加载 React 热更新运行时。
  page = await context.newPage();
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toHaveAttribute('data-hmr-acceptance', 'version-1', { timeout: 45000 });
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
  expect(scriptPaths.filter(path => path === '/src/main.ts')).toHaveLength(1);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error?.stack ?? error);
  if (page) {
    try { await page.screenshot({ path: join(run.artifactsDir, 'development-hmr-failure.png') }); }
    catch (diagnosticError) { failure = new AggregateError([failure, diagnosticError], 'HMR 验证及截图均失败'); }
  }
} finally {
  try { await run.close(); }
  catch (cleanupError) { failure = failure ? new AggregateError([failure, cleanupError], 'HMR 验证及清理均失败') : cleanupError; }
  await writeFile(required('BOXTEAM_BROWSER_RESULT_PATH'), JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
