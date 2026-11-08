import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const required = name => { if (!process.env[name]) throw new Error('缺少 ' + name); return process.env[name]; };
const fixture = JSON.parse(required('BOXTEAM_EMPTY_TARGET_WEB'));
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = await TestRunContext.fromTestFile('tests/integration/clients/web/test_empty_target_web.py').prepare();
const observations = () => ({ pageErrors: [], consoleErrors: [], nodeChannels: [], nodeResolves: [], webAdmissions: [],
  targetDirectories: [], inventoryRequests: [], themeResources: [] });
const result = { phase: 'initialize', ...observations(), receipts: [],
  boundary: '正式默认Workbench／双真实Gateway和Backend／原Node管理安装器／原目标资格、iframe、Worker、公开扩展命令及主题选择器；只将SSH执行及隧道替换为既有loopback装具。安装由正式API生成，页面不注入运行描述或命令。两个独立BrowserContext验证零boxteam-file根的browser与纯声明式主题，不代表公网市场、真实SSH、Windows、Electron或Node扩展运行。' };
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const phase = async value => { result.phase = value; await save(); };
let page, diagnosticPage, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-extensions', '--renderer-process-limit=2'] });
  run.addCleanup('空根目标 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1700, height: 1100 } });
  run.addCleanup('空根目标 BrowserContext', () => context.close());
  page = await context.newPage();
  const observe = (view, state) => {
    view.on('pageerror', error => state.pageErrors.push(String(error.stack ?? error)));
    view.on('console', entry => { if (entry.type() === 'error') state.consoleErrors.push(entry.text()); });
    view.on('response', response => {
      const path = new URL(response.url()).pathname;
      if (path.endsWith('/boxteam-probe.empty-theme/theme.json')) state.themeResources.push({ path, status: response.status(),
        requestId: response.headers()['x-request-id'], phase: result.phase });
    });
    view.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/gateway/extension-hosts/resolve') state.nodeResolves.push(request.postDataJSON());
      if (path === '/api/gateway/extension-targets') state.targetDirectories.push(request.postDataJSON());
      if (path === '/api/gateway/extension-installations/inventory') state.inventoryRequests.push(request.postDataJSON());
    });
    view.on('websocket', socket => {
      const path = new URL(socket.url()).pathname;
      if (path === '/api/gateway/extension-hosts/channel') state.nodeChannels.push(path);
      if (path !== '/api/gateway/extension-web-hosts/channel') return;
      socket.on('framesent', event => {
        if (typeof event.payload === 'string') state.webAdmissions.push({ kind: 'open', value: JSON.parse(event.payload) });
      });
      socket.on('framereceived', event => {
        if (typeof event.payload !== 'string') return;
        const value = JSON.parse(event.payload);
        state.webAdmissions.push({ kind: 'qualified', value: { protocol_version: value.protocol_version, gateway_id: value.gateway_id,
          backend_id: value.backend_id, profile_id: value.profile_id, view_id: value.view_id, generation: value.generation,
          workspace_ids: value.workspace_ids, extension_keys: value.extension_keys, request_id: value.request_id } });
      });
    });
  };
  observe(page, result); diagnosticPage = page;
  const command = (view, id, ...args) => view.evaluate(async ({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = async (view, path, method = 'GET', payload) => {
    const receipt = await view.evaluate(async ({ path, method, payload }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token },
        ...(payload ? { body: JSON.stringify(payload) } : {}) });
      return { status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, method, payload });
    result.receipts.push(receipt);
    expect(receipt.status).toBe(200); expect(receipt.body.request_id).toBeTruthy(); expect(receipt.body.request_id).toBe(receipt.header);
    return receipt.body.data;
  };
  const initialize = async (view, base, target) => {
    await view.goto(new URL('/api/gateway/health', base).href);
    await api(view, '/api/gateway/users/guest', 'POST', { tracking: { source: 'empty-target-web' } });
    // 默认产品入口保留虚拟Workbench根；不传folder/workspace参数，也不挂接工作区文件根。
    await view.goto(base, { waitUntil: 'domcontentloaded' });
    expect(new URL(view.url()).search).toBe('');
    await expect(view.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
    const health = await api(view, '/api/gateway/health'), workspaces = await api(view, '/api/gateway/workspaces');
    expect(workspaces.active_workspace.gateway_id).toBe(health.gateway_id);
    expect(health.gateway_id).toBe(target.gateway_id);
    await api(view, '/api/v1/workspace');
  };
  const admission = (state, target, count) => {
    const values = state.webAdmissions.filter(item => item.kind === 'qualified');
    expect(values).toHaveLength(count);
    expect(state.webAdmissions.filter(item => item.kind === 'open')).toHaveLength(count);
    for (const { value } of values) {
      expect(value).toMatchObject({ ...target, workspace_ids: [], view_id: 'default' });
      expect(value.extension_keys).toHaveLength(2);
      for (const id of ['empty-browser', 'empty-theme']) expect(value.extension_keys.some(key => key.startsWith('boxteam-probe.' + id + '-'))).toBe(true);
    }
    expect(state.nodeResolves).toEqual([]); expect(state.nodeChannels).toEqual([]);
    return values.at(-1).value;
  };
  const themeColor = view => view.evaluate(() => getComputedStyle(document.querySelector('.monaco-workbench'))
    .getPropertyValue('--vscode-editor-background').trim());
  const themeResource = (state, connection) => {
    const key = connection.extension_keys.find(key => key.startsWith('boxteam-probe.empty-theme-'));
    const path = '/api/gateway/extension-packages/content/' + [connection.gateway_id, connection.backend_id, connection.profile_id,
      connection.view_id, connection.generation, key, 'boxteam-probe.empty-theme', 'theme.json'].join('/');
    return state.themeResources.some(resource => resource.path === path && resource.status === 200 && !!resource.requestId);
  };
  const selectTheme = async view => {
    const selected = command(view, 'workbench.action.selectTheme');
    const input = view.locator('.quick-input-widget:visible input');
    await expect(input).toBeVisible(); await input.fill(fixture.theme.label);
    await expect(view.locator('.quick-input-widget:visible').getByText(fixture.theme.label, { exact: true })).toBeVisible();
    await input.press('Enter'); await selected;
  };
  await initialize(page, fixture.base_url, fixture.targets.a);

  await phase('run-entry-a-empty-web');
  result.before = await command(page, 'boxteam.empty.inspect', 'before-management-switch');
  expect(result.before).toMatchObject({ value: 'before-management-switch', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.before.extensionUri).toContain('/api/gateway/extension-packages/content/' + fixture.targets.a.gateway_id + '/');
  expect(result.before.extensionUri).toContain('/' + fixture.targets.a.backend_id + '/');
  expect(result.nodeResolves).toEqual([]); expect(result.nodeChannels).toEqual([]);
  const initialAdmission = admission(result, fixture.targets.a, 1);
  expect(result.targetDirectories[0]).toEqual({ protocol_version: 1, gateway_id: fixture.targets.a.gateway_id });

  await phase('select-entry-a-declarative-theme');
  await selectTheme(page);
  await expect.poll(() => themeColor(page)).toBe(fixture.theme.colors.a);
  await expect.poll(() => themeResource(result, initialAdmission)).toBe(true);
  result.theme = { selected: await themeColor(page) };

  await phase('switch-management-b-retain-running-a');
  await command(page, 'workbench.view.extensions');
  const targetAction = page.getByRole('button').filter({ hasText: /^目标：/ });
  await targetAction.click();
  const picker = page.locator('.quick-input-widget:visible');
  await expect(picker).toBeVisible();
  await picker.locator('.monaco-list-row').filter({ hasText: 'Empty Target B' }).click();
  await expect(picker).toBeHidden(); await expect(targetAction).toHaveText('目标：Empty Target B');
  await expect.poll(() => result.inventoryRequests.some(item => item.gateway_id === fixture.targets.b.gateway_id
    && item.backend_id === fixture.targets.b.backend_id)).toBe(true);
  result.after = await command(page, 'boxteam.empty.inspect', 'after-management-switch');
  expect(result.after).toMatchObject({ value: 'after-management-switch', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.after.activation).toBe(result.before.activation); expect(result.after.extensionUri).toBe(result.before.extensionUri);
  admission(result, fixture.targets.a, 1);
  result.theme.afterManagementSwitch = await themeColor(page);
  expect(result.theme.afterManagementSwitch).toBe(fixture.theme.colors.a);
  expect(await page.locator('.quick-input-widget:visible').count()).toBe(0);

  await phase('reload-entry-a-regenerates-target-view');
  await page.reload({ waitUntil: 'domcontentloaded' });
  // 重载沿原布局恢复 Extensions 侧边栏，不要求会话导航抢回当前视图。
  await expect(page.locator('.monaco-workbench')).toBeVisible({ timeout: 30_000 });
  await page.waitForFunction(() => typeof globalThis.workbench?.commands?.executeCommand === 'function', null, { timeout: 30_000 });
  result.reloaded = await command(page, 'boxteam.empty.inspect', 'after-reload');
  expect(result.reloaded).toMatchObject({ value: 'after-reload', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.reloaded.activation).not.toBe(result.before.activation);
  const renewedAdmission = admission(result, fixture.targets.a, 2);
  expect(renewedAdmission.generation).not.toBe(initialAdmission.generation);
  expect(result.reloaded.extensionUri).toContain('/api/gateway/extension-packages/content/' + fixture.targets.a.gateway_id + '/'
    + fixture.targets.a.backend_id + '/' + renewedAdmission.profile_id + '/default/' + renewedAdmission.generation + '/');
  await expect.poll(() => themeColor(page), { timeout: 30_000 }).toBe(fixture.theme.colors.a);
  await expect.poll(() => themeResource(result, renewedAdmission)).toBe(true);
  result.theme.restored = await themeColor(page);

  await phase('independent-client-direct-entry-b');
  const contextB = await browser.newContext({ viewport: { width: 1700, height: 1100 } });
  run.addCleanup('独立B BrowserContext', () => contextB.close());
  const pageB = await contextB.newPage(); diagnosticPage = pageB;
  result.secondClient = observations(); observe(pageB, result.secondClient);
  await initialize(pageB, fixture.base_url_b, fixture.targets.b);
  result.secondClient.inspection = await command(pageB, 'boxteam.empty.inspect', 'direct-entry-b');
  expect(result.secondClient.inspection).toMatchObject({ value: 'direct-entry-b', version: '2.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.secondClient.inspection.activation).not.toBe(result.reloaded.activation);
  const admissionB = admission(result.secondClient, fixture.targets.b, 1);
  expect(result.secondClient.inspection.extensionUri).toContain('/api/gateway/extension-packages/content/' + fixture.targets.b.gateway_id + '/'
    + fixture.targets.b.backend_id + '/' + admissionB.profile_id + '/default/' + admissionB.generation + '/');
  expect(result.secondClient.targetDirectories[0]).toEqual({ protocol_version: 1, gateway_id: fixture.targets.b.gateway_id });
  await selectTheme(pageB);
  await expect.poll(() => themeColor(pageB)).toBe(fixture.theme.colors.b);
  await expect.poll(() => themeResource(result.secondClient, admissionB)).toBe(true);
  result.secondClient.themeColor = await themeColor(pageB);
  await pageB.screenshot({ path: output.replace(/\.json$/, '.b.png'), fullPage: true });

  diagnosticPage = page;
  result.afterSecondClient = await command(page, 'boxteam.empty.inspect', 'after-independent-b');
  expect(result.afterSecondClient).toMatchObject({ value: 'after-independent-b', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.afterSecondClient.activation).toBe(result.reloaded.activation);
  expect(result.afterSecondClient.extensionUri).toBe(result.reloaded.extensionUri);
  expect(await themeColor(page)).toBe(fixture.theme.colors.a);
  admission(result, fixture.targets.a, 2); admission(result.secondClient, fixture.targets.b, 1);
  for (const state of [result, result.secondClient]) {
    expect(state.pageErrors).toEqual([]);
    expect(state.themeResources.every(resource => resource.status === 200 && !!resource.requestId)).toBe(true);
  }
  await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
  await phase('complete');
} catch (error) {
  failure = error; result.error = String(error.stack ?? error);
  if (diagnosticPage) {
    result.visible = await diagnosticPage.locator('body').innerText().catch(error => String(error));
    await diagnosticPage.screenshot({ path: output.replace(/\.json$/, '.failed.png'), fullPage: true }).catch(error => { result.screenshotError = String(error); });
    await writeFile(output.replace(/\.json$/, '.failed.html'), await diagnosticPage.content()).catch(error => { result.snapshotError = String(error); });
  }
} finally {
  try { await save(); } catch (error) { failure = failure ? new AggregateError([failure, error], '空根验收及证据保存失败') : error; }
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '空根验收及清理失败') : error; }
}
if (failure) throw failure;
