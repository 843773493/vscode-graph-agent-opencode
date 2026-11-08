import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const required = name => { if (!process.env[name]) throw new Error('缺少 ' + name); return process.env[name]; };
const fixture = JSON.parse(required('BOXTEAM_EMPTY_TARGET_WEB'));
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = await TestRunContext.fromTestFile('tests/integration/clients/web/test_empty_target_web.py').prepare();
const result = { phase: 'initialize', pageErrors: [], consoleErrors: [], nodeChannels: [], nodeResolves: [], webAdmissions: [],
  receipts: [], targetDirectories: [], inventoryRequests: [],
  boundary: '正式默认Workbench／双真实Gateway和Backend／原Node管理安装器／原目标资格、iframe、Worker和公开扩展命令；只将SSH执行及隧道替换为既有loopback装具。安装由正式API生成，页面不注入运行描述或命令。验证零boxteam-file根的browser运行，不代表公网市场、真实SSH、Windows、Electron或Node扩展运行。' };
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const phase = async value => { result.phase = value; await save(); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-extensions', '--renderer-process-limit=2'] });
  run.addCleanup('空根目标 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1700, height: 1100 } });
  run.addCleanup('空根目标 BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error.stack ?? error)));
  page.on('console', entry => { if (entry.type() === 'error') result.consoleErrors.push(entry.text()); });
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/extension-hosts/resolve') result.nodeResolves.push(request.postDataJSON());
    if (path === '/api/gateway/extension-targets') result.targetDirectories.push(request.postDataJSON());
    if (path === '/api/gateway/extension-installations/inventory') result.inventoryRequests.push(request.postDataJSON());
  });
  page.on('websocket', socket => {
    const path = new URL(socket.url()).pathname;
    if (path === '/api/gateway/extension-hosts/channel') result.nodeChannels.push(path);
    if (path !== '/api/gateway/extension-web-hosts/channel') return;
    socket.on('framesent', event => {
      if (typeof event.payload === 'string') result.webAdmissions.push({ kind: 'open', value: JSON.parse(event.payload) });
    });
    socket.on('framereceived', event => {
      if (typeof event.payload !== 'string') return;
      const value = JSON.parse(event.payload);
      result.webAdmissions.push({ kind: 'qualified', value: { protocol_version: value.protocol_version, gateway_id: value.gateway_id,
        backend_id: value.backend_id, profile_id: value.profile_id, view_id: value.view_id, generation: value.generation,
        workspace_ids: value.workspace_ids, extension_keys: value.extension_keys, request_id: value.request_id } });
    });
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = async (path, method = 'GET', payload) => {
    const receipt = await page.evaluate(async ({ path, method, payload }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token },
        ...(payload ? { body: JSON.stringify(payload) } : {}) });
      return { status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, method, payload });
    result.receipts.push(receipt);
    expect(receipt.status).toBe(200); expect(receipt.body.request_id).toBeTruthy(); expect(receipt.body.request_id).toBe(receipt.header);
    return receipt.body.data;
  };
  await page.goto(new URL('/api/gateway/health', fixture.base_url).href);
  await api('/api/gateway/users/guest', 'POST', { tracking: { source: 'empty-target-web' } });
  // 默认产品入口保留虚拟Workbench根；不传folder/workspace参数，也不挂接工作区文件根。
  await page.goto(fixture.base_url, { waitUntil: 'domcontentloaded' });
  expect(new URL(page.url()).search).toBe('');
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  expect(workspaces.active_workspace.gateway_id).toBe(health.gateway_id);
  expect(health.gateway_id).toBe(fixture.targets.a.gateway_id);
  await api('/api/v1/workspace');

  await phase('run-entry-a-empty-web');
  result.before = await command('boxteam.empty.inspect', 'before-management-switch');
  expect(result.before).toMatchObject({ value: 'before-management-switch', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.before.extensionUri).toContain('/api/gateway/extension-packages/content/' + fixture.targets.a.gateway_id + '/');
  expect(result.before.extensionUri).toContain('/' + fixture.targets.a.backend_id + '/');
  expect(result.nodeResolves).toEqual([]); expect(result.nodeChannels).toEqual([]);
  const qualified = result.webAdmissions.filter(item => item.kind === 'qualified');
  expect(qualified).toHaveLength(1);
  expect(qualified[0].value).toMatchObject({ ...fixture.targets.a, workspace_ids: [], view_id: 'default' });
  expect(qualified[0].value.extension_keys).toHaveLength(1);
  expect(qualified[0].value.extension_keys[0]).toContain('boxteam-probe.empty-browser');
  expect(result.targetDirectories[0]).toEqual({ protocol_version: 1, gateway_id: fixture.targets.a.gateway_id });

  await phase('switch-management-b-retain-running-a');
  await command('workbench.view.extensions');
  const targetAction = page.getByRole('button').filter({ hasText: /^目标：/ });
  await targetAction.click();
  const picker = page.locator('.quick-input-widget:visible');
  await expect(picker).toBeVisible();
  await picker.locator('.monaco-list-row').filter({ hasText: 'Empty Target B' }).click();
  await expect(picker).toBeHidden(); await expect(targetAction).toHaveText('目标：Empty Target B');
  await expect.poll(() => result.inventoryRequests.some(item => item.gateway_id === fixture.targets.b.gateway_id
    && item.backend_id === fixture.targets.b.backend_id)).toBe(true);
  result.after = await command('boxteam.empty.inspect', 'after-management-switch');
  expect(result.after).toMatchObject({ value: 'after-management-switch', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.after.activation).toBe(result.before.activation); expect(result.after.extensionUri).toBe(result.before.extensionUri);
  expect(result.webAdmissions.filter(item => item.kind === 'qualified')).toHaveLength(1);
  expect(result.nodeResolves).toEqual([]); expect(result.nodeChannels).toEqual([]);
  expect(await page.locator('.quick-input-widget:visible').count()).toBe(0);
  await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
  await phase('complete');
} catch (error) {
  failure = error; result.error = String(error.stack ?? error);
  if (page) {
    result.visible = await page.locator('body').innerText().catch(error => String(error));
    await page.screenshot({ path: output.replace(/\.json$/, '.failed.png'), fullPage: true }).catch(error => { result.screenshotError = String(error); });
    await writeFile(output.replace(/\.json$/, '.failed.html'), await page.content()).catch(error => { result.snapshotError = String(error); });
  }
} finally {
  try { await save(); } catch (error) { failure = failure ? new AggregateError([failure, error], '空根验收及证据保存失败') : error; }
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '空根验收及清理失败') : error; }
}
if (failure) throw failure;
