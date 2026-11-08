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
  boundary: '正式默认Workbench／双真实Gateway和Backend／原Node管理安装器／原Web Worker及目标Node宿主。原API安装同包双入口及纯声明式主题；原挂接命令生成运行视图，两个BrowserContext核库存、隔离和清缓存恢复。SSH执行及隧道仍为loopback装具，不代表公网市场、真实SSH、Windows或Electron。' };
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
  const webTransports = [];
  // 只持有原 WebSocket 的网络故障注入口；Worker、握手和后续恢复仍由产品创建。
  await context.routeWebSocket('**/api/gateway/extension-web-hosts/channel*', socket => {
    webTransports.push({ client: socket, server: socket.connectToServer() });
  });
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
  const ready = async view => {
    await expect(view.locator('.monaco-workbench')).toBeVisible({ timeout: 30_000 });
    await view.waitForFunction(() => typeof globalThis.workbench?.commands?.executeCommand === 'function', null, { timeout: 30_000 });
  };
  const initialize = async (view, base, target) => {
    await view.goto(new URL('/api/gateway/health', base).href);
    await api(view, '/api/gateway/users/guest', 'POST', { tracking: { source: 'empty-target-web' } });
    // 默认产品入口保留虚拟Workbench根；不传folder/workspace参数，也不挂接工作区文件根。
    await view.goto(base, { waitUntil: 'domcontentloaded' });
    expect(new URL(view.url()).search).toBe('');
    await ready(view);
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
      expect(value.extension_keys).toHaveLength(3);
      for (const id of ['empty-browser', 'empty-theme', 'empty-hybrid']) expect(value.extension_keys.some(key => key.startsWith('boxteam-probe.' + id + '-'))).toBe(true);
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
  const inventory = async (view, target) => {
    const data = await api(view, '/api/gateway/extension-installations/inventory', 'POST', { protocol_version: 1, ...target });
    expect(data).toMatchObject({ protocol_version: 1, ...target, profile_scope: 'default' });
    return data.inventory;
  };
  const webHybrid = async (view, version, connection) => {
    const value = await command(view, 'boxteam.empty.hybrid');
    expect(value).toMatchObject({ mode: 'web', version, worker: true, folders: [] });
    expect(value.extensionUri).toContain('/' + connection.gateway_id + '/' + connection.backend_id + '/' + connection.profile_id
      + '/' + connection.view_id + '/' + connection.generation + '/boxteam-probe.empty-hybrid-' + version + '/');
    return value;
  };
  const runtimeDiagnostics = async (view, state, target, id, kind, scope) => {
    const before = JSON.stringify([state.nodeResolves, state.nodeChannels, state.webAdmissions]);
    const report = await command(view, id);
    expect(report.scope).toBe(scope); expect(report.version).toBe('1.145.0');
    expect(report.targets).toHaveLength(1);
    const [diagnostic] = report.targets;
    expect(diagnostic.installationTarget).toEqual({ kind: 'backend', gatewayId: target.gateway_id, backendId: target.backend_id });
    expect(diagnostic.viewId).toBe('default');
    for (const api of ['notebooks', 'terminal', 'tasks', 'debug', 'secrets', 'stateSync']) expect(report.apiSupport.find(entry => entry.id === api).state).toBe('unsupported');
    expect(report.apiSupport.find(entry => entry.id === 'documentsLanguageSearch').state).toBe('partial');
    expect(diagnostic.hosts.length).toBeGreaterThan(0);
    if (scope === 'caller') expect(diagnostic.hosts).toHaveLength(1);
    const host = diagnostic.hosts.find(host => host.kind === kind);
    expect(host.phase).toBe('bound'); expect(host.binding.generation).toBeTruthy(); expect(host.binding.requestId).toBeTruthy();
    expect(host.extensions.some(extension => extension.activationStarted && extension.activated)).toBe(true);
    expect(JSON.stringify([state.nodeResolves, state.nodeChannels, state.webAdmissions])).toBe(before);
    return report;
  };
  const notebooks = async (view, id) => {
    const value = await command(view, id);
    const names = [
      ...['activeNotebookEditor', 'onDidChangeActiveNotebookEditor', 'visibleNotebookEditors', 'onDidChangeVisibleNotebookEditors',
        'onDidChangeNotebookEditorSelection', 'onDidChangeNotebookEditorVisibleRanges', 'showNotebookDocument'].map(name => 'window.' + name),
      ...['notebookDocuments', 'openNotebookDocument', 'onDidSaveNotebookDocument', 'onDidChangeNotebookDocument',
        'onWillSaveNotebookDocument', 'onDidOpenNotebookDocument', 'onDidCloseNotebookDocument', 'registerNotebookSerializer'].map(name => 'workspace.' + name),
      ...['createNotebookController', 'registerNotebookCellStatusBarItemProvider', 'createRendererMessaging',
        'createNotebookControllerDetectionTask', 'registerKernelSourceActionProvider'].map(name => 'notebooks.' + name),
    ].sort();
    expect(value.rejected.map(item => item.api).sort()).toEqual(names);
    for (const item of value.rejected) {
      expect(item.accepted).toBeUndefined();
      expect(item.error).toContain('目标扩展暂不支持 Notebook API');
    }
    expect(value.values).toEqual({ language: 'python', count: 1, metadata: { probe: true } });
    return value;
  };
  await initialize(page, fixture.base_url, fixture.targets.a);

  await phase('run-entry-a-empty-web');
  result.before = await command(page, 'boxteam.empty.inspect', 'before-management-switch');
  expect(result.before).toMatchObject({ value: 'before-management-switch', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.before.extensionUri).toContain('/api/gateway/extension-packages/content/' + fixture.targets.a.gateway_id + '/');
  expect(result.before.extensionUri).toContain('/' + fixture.targets.a.backend_id + '/');
  expect(result.nodeResolves).toEqual([]); expect(result.nodeChannels).toEqual([]);
  const initialAdmission = admission(result, fixture.targets.a, 1);
  result.inventoryA = await inventory(page, fixture.targets.a);
  result.hybrid = await webHybrid(page, '1.0.0', initialAdmission);
  result.runtimeDiagnostics = await runtimeDiagnostics(page, result, fixture.targets.a,
    'boxteam.extensions.getRuntimeDiagnostics', 'web', 'window');
  result.callerDiagnostics = await runtimeDiagnostics(page, result, fixture.targets.a,
    'boxteam.empty.runtimeDiagnostics', 'web', 'caller');
  result.notebooks = await notebooks(page, 'boxteam.empty.webNotebooks');
  expect(result.runtimeDiagnostics.targets[0].nodeChannel).toBeNull();
  expect(result.callerDiagnostics.targets[0].hosts[0].binding.generation).toBe(initialAdmission.generation);
  expect(result.targetDirectories[0]).toEqual({ protocol_version: 1, gateway_id: fixture.targets.a.gateway_id });

  await phase('node-only-unavailable-in-empty-window');
  result.nodeOnlyRejected = await command(page, 'boxteam.empty.nodeOnly').then(value => ({ value }), error => ({ error: String(error) }));
  expect(result.nodeOnlyRejected.error).toContain('boxteam.empty.nodeOnly');
  await command(page, 'workbench.view.extensions');
  const nodeRow = page.getByRole('listitem', { name: /^empty-node-only, / });
  await expect(nodeRow).toBeVisible(); await nodeRow.dblclick();
  const unavailable = '此扩展当前不可运行：Node 扩展需要挂接该目标的工作区。';
  await expect(page.getByText(unavailable, { exact: true })).toBeVisible();
  await expect(nodeRow).not.toContainText('Restart Required');
  result.nodeOnlyStatus = unavailable;

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
  result.relayInventoryB = await inventory(page, fixture.targets.b);
  result.after = await command(page, 'boxteam.empty.inspect', 'after-management-switch');
  result.afterManagementDiagnostics = await runtimeDiagnostics(page, result, fixture.targets.a,
    'boxteam.empty.runtimeDiagnostics', 'web', 'caller');
  expect(result.afterManagementDiagnostics.targets[0].hosts[0].binding.generation).toBe(initialAdmission.generation);
  expect(result.after).toMatchObject({ value: 'after-management-switch', version: '1.0.0', folders: [], webWorker: true, uiKind: 2 });
  expect(result.after.activation).toBe(result.before.activation); expect(result.after.extensionUri).toBe(result.before.extensionUri);
  admission(result, fixture.targets.a, 1);
  result.theme.afterManagementSwitch = await themeColor(page);
  expect(result.theme.afterManagementSwitch).toBe(fixture.theme.colors.a);
  expect(await page.locator('.quick-input-widget:visible').count()).toBe(0);

  await phase('reload-entry-a-regenerates-target-view');
  await page.reload({ waitUntil: 'domcontentloaded' });
  // 重载沿原布局恢复 Extensions 侧边栏，不要求会话导航抢回当前视图。
  await ready(page);
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
  result.secondClient.inventory = await inventory(pageB, fixture.targets.b);
  expect(result.secondClient.inventory).toEqual(result.relayInventoryB);
  result.secondClient.hybrid = await webHybrid(pageB, '2.0.0', admissionB);
  result.secondClient.runtimeDiagnostics = await runtimeDiagnostics(pageB, result.secondClient, fixture.targets.b,
    'boxteam.empty.runtimeDiagnostics', 'web', 'caller');
  expect(result.secondClient.runtimeDiagnostics.targets[0].hosts[0].binding.generation).toBe(admissionB.generation);
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

  await phase('clear-client-cache-and-recover-target-installations');
  await page.close();
  page = await context.newPage(); diagnosticPage = page;
  await page.goto(new URL('/api/gateway/health', fixture.base_url).href);
  const databases = () => page.evaluate(async () => (await indexedDB.databases()).map(database => database.name).sort());
  result.cacheRecovery = { ...observations(), databasesBefore: await databases() };
  expect(result.cacheRecovery.databasesBefore.length).toBeGreaterThan(0);
  const cdp = await context.newCDPSession(page);
  try {
    await cdp.send('Storage.clearDataForOrigin', { origin: new URL(fixture.base_url).origin,
      storageTypes: 'indexeddb,cache_storage,local_storage,service_workers' });
    await cdp.send('Network.clearBrowserCache');
  } finally { await cdp.detach(); }
  result.cacheRecovery.databasesAfterClear = await databases();
  expect(result.cacheRecovery.databasesAfterClear).toEqual([]);
  observe(page, result.cacheRecovery);
  await initialize(page, fixture.base_url, fixture.targets.a);
  result.cacheRecovery.inspection = await command(page, 'boxteam.empty.inspect', 'after-cache-clear');
  expect(result.cacheRecovery.inspection).toMatchObject({ version: '1.0.0', folders: [], webWorker: true });
  expect(result.cacheRecovery.inspection.activation).not.toBe(result.reloaded.activation);
  const recovered = admission(result.cacheRecovery, fixture.targets.a, 1);
  result.cacheRecovery.hybrid = await webHybrid(page, '1.0.0', recovered);
  result.cacheRecovery.inventory = await inventory(page, fixture.targets.a);
  expect(result.cacheRecovery.inventory).toEqual(result.inventoryA);
  await selectTheme(page);
  await expect.poll(() => themeColor(page)).toBe(fixture.theme.colors.a);
  await expect.poll(() => themeResource(result.cacheRecovery, recovered)).toBe(true);
  await page.close();

  await phase('attach-real-workspace-selects-same-package-node-entry');
  page = await context.newPage(); diagnosticPage = page;
  result.attached = observations(); observe(page, result.attached);
  await initialize(page, fixture.base_url, fixture.targets.a);
  await command(page, 'boxteam.empty.hybrid');
  expect(result.attached.nodeResolves).toEqual([]); expect(result.attached.nodeChannels).toEqual([]);
  const navigation = page.waitForURL(url => url.searchParams.has('workspace'), { waitUntil: 'domcontentloaded', timeout: 30_000 });
  const attaching = command(page, 'boxteam.extensions.attachWorkspaceViews');
  const workspacePicker = page.locator('.quick-input-widget:visible');
  await expect(workspacePicker).toBeVisible();
  await workspacePicker.locator('.monaco-list-row').filter({ hasText: fixture.targets.a.gateway_id }).locator('.monaco-checkbox').click();
  await workspacePicker.locator('input').press('Enter');
  await attaching.catch(error => { if (!String(error).includes('Execution context was destroyed')) throw error; });
  await navigation; await ready(page);
  result.attached.url = page.url();
  result.attached.hybrid = await command(page, 'boxteam.empty.hybrid');
  expect(result.attached.hybrid).toMatchObject({ mode: 'node', version: '1.0.0', platform: fixture.native_platform,
    nativeContent: 'hybrid-node-1.0.0', folders: [fixture.native_root_a] });
  result.attached.nodeOnly = await command(page, 'boxteam.empty.nodeOnly');
  result.attached.runtimeDiagnostics = await runtimeDiagnostics(page, result.attached, fixture.targets.a,
    'boxteam.extensions.getRuntimeDiagnostics', 'node', 'window');
  result.attached.nodeDiagnostics = await runtimeDiagnostics(page, result.attached, fixture.targets.a,
    'boxteam.empty.nodeDiagnostics', 'node', 'caller');
  result.attached.notebooks = await notebooks(page, 'boxteam.empty.nodeNotebooks');
  await phase('scoped-symbol-provider-failure');
  result.attached.webSymbols = await command(page, 'boxteam.empty.inspect', 'activate-original-symbol-provider');
  expect(result.attached.webSymbols.folders).toHaveLength(1);
  await command(page, 'workbench.action.quickOpen', '#ScopedSymbolProbe');
  const symbols = page.locator('.quick-input-widget:visible');
  await expect(symbols.locator('.monaco-list-row').filter({ hasText: '目标 Web 原 Provider' })).toBeVisible();
  const symbolFailure = symbols.locator('.monaco-list-row').filter({ hasText: '目标 Node 符号故障探针' });
  await expect(symbolFailure).toBeVisible();
  await expect(symbolFailure).toContainText(fixture.targets.a.gateway_id);
  await expect(symbolFailure).toContainText(fixture.targets.a.backend_id);
  result.attached.symbolFailure = { success: await symbols.locator('.monaco-list-row').filter({ hasText: '目标 Web 原 Provider' }).innerText(),
    failure: await symbolFailure.innerText() };
  await symbols.locator('input').press('Escape');
  expect(result.attached.runtimeDiagnostics.targets[0].hosts.map(host => host.kind).sort()).toEqual(['node', 'web']);
  expect(result.attached.nodeDiagnostics.targets[0].hosts[0].processApi.state).toBe('partial');
  expect(result.attached.nodeDiagnostics.targets[0].nodeChannel.state).toBe('available');
  expect(result.attached.nodeOnly).toEqual({ mode: 'node', version: '1.0.0' });
  expect(result.attached.nodeResolves).toEqual([{ protocol_version: 1, ...fixture.targets.a, workspace_ids: [fixture.workspace_id] }]);
  expect(result.attached.nodeChannels).toEqual(['/api/gateway/extension-hosts/channel']);
  const attachedAdmission = result.attached.webAdmissions.filter(item => item.kind === 'qualified').at(-1).value;
  expect(attachedAdmission).toMatchObject({ ...fixture.targets.a, workspace_ids: [fixture.workspace_id] });
  expect(attachedAdmission.extension_keys).toEqual(['boxteam-probe.empty-browser-1.0.0', 'boxteam-probe.empty-theme-1.0.0']);
  expect(await inventory(page, fixture.targets.a)).toEqual(result.inventoryA);
  await selectTheme(page);
  await expect.poll(() => themeColor(page)).toBe(fixture.theme.colors.a);
  await expect.poll(() => themeResource(result.attached, attachedAdmission)).toBe(true);
  await phase('web-crash-recovery');
  const failedWeb = webTransports.at(-1);
  expect(failedWeb).toBeTruthy();
  await Promise.all([failedWeb.client, failedWeb.server].map(socket => socket.close({
    code: 1011, reason: 'Web qualification transport lost',
  })));
  result.webCrash = {};
  await expect.poll(async () => {
    const report = await command(page, 'boxteam.extensions.getRuntimeDiagnostics');
    const host = report.targets[0].hosts.find(host => host.kind === 'web');
    result.webCrash.diagnostics = report;
    return host?.phase === 'bound' && host.binding.generation !== attachedAdmission.generation
      && host.extensions.some(extension => extension.activated);
  }, { timeout: 30_000 }).toBe(true);
  result.webCrash.inspection = await command(page, 'boxteam.empty.inspect', 'after-web-crash');
  expect(result.webCrash.inspection).toMatchObject({ version: '1.0.0', folders: result.attached.webSymbols.folders });
  expect(result.webCrash.inspection.activation).not.toBe(result.attached.webSymbols.activation);
  const recoveredAdmission = result.attached.webAdmissions.filter(item => item.kind === 'qualified').at(-1).value;
  expect(recoveredAdmission.generation).not.toBe(attachedAdmission.generation);
  expect(recoveredAdmission.extension_keys).toEqual(attachedAdmission.extension_keys);
  expect(result.webCrash.inspection.extensionUri).toContain('/' + recoveredAdmission.generation + '/');
  await expect.poll(() => themeColor(page), { timeout: 30_000 }).toBe(fixture.theme.colors.a);
  await expect.poll(() => themeResource(result.attached, recoveredAdmission)).toBe(true);
  expect(await command(page, 'boxteam.empty.hybrid')).toEqual(result.attached.hybrid);
  await phase('node-crash-recovery');
  result.crashes = [];
  let previousNode = result.attached.hybrid;
  let previousGeneration = result.attached.nodeDiagnostics.targets[0].hosts[0].binding.generation;
  for (let crash = 1; crash <= 3; crash++) {
    // 经真实扩展命令退出实际 Node；恢复只重激活扩展，不重放这次未知 RPC。
    const receipt = await command(page, 'boxteam.empty.crashNode').then(value => ({ value }), error => ({ error: String(error) }));
    expect(receipt.error).toBeTruthy();
    const observation = { crash, receipt };
    result.crashes.push(observation);
    if (crash === 3) {
      const notification = page.getByRole('dialog', { name: /扩展宿主在 5 分钟内已崩溃 3 次，停止自动重启/ });
      await expect(notification).toBeVisible({ timeout: 30_000 });
      observation.notification = await notification.innerText();
      observation.diagnostics = await command(page, 'boxteam.extensions.getRuntimeDiagnostics');
      expect(observation.diagnostics.targets[0].hosts.map(host => host.kind)).toEqual(['web']);
      observation.web = await command(page, 'boxteam.empty.inspect', 'after-node-restart-limit');
      expect(observation.web.activation).toBe(result.webCrash.inspection.activation);
      expect(observation.web.extensionUri).toBe(result.webCrash.inspection.extensionUri);
      expect(result.attached.nodeChannels).toHaveLength(3);
      break;
    }
    let recovered;
    await expect.poll(async () => {
      recovered = await command(page, 'boxteam.extensions.getRuntimeDiagnostics');
      const host = recovered.targets[0].hosts.find(host => host.kind === 'node');
      return host?.phase === 'bound' && host.binding.generation !== previousGeneration
        && host.extensions.some(extension => extension.activated);
    }, { timeout: 30_000 }).toBe(true);
    observation.diagnostics = recovered;
    const current = await command(page, 'boxteam.empty.hybrid');
    expect(current).toMatchObject({ mode: 'node', version: '1.0.0', nativeContent: 'hybrid-node-1.0.0',
      folders: [fixture.native_root_a], activation: previousNode.activation + 1 });
    expect(current.pid).not.toBe(previousNode.pid);
    observation.node = current;
    const web = await command(page, 'boxteam.empty.inspect', 'after-node-crash-' + crash);
    expect(web.activation).toBe(result.webCrash.inspection.activation);
    expect(web.extensionUri).toBe(result.webCrash.inspection.extensionUri);
    observation.web = web;
    previousNode = current;
    previousGeneration = recovered.targets[0].hosts.find(host => host.kind === 'node').binding.generation;
  }
  expect(await inventory(page, fixture.targets.a)).toEqual(result.inventoryA);
  for (const state of [result.cacheRecovery, result.attached]) expect(state.pageErrors).toEqual([]);
  await page.screenshot({ path: output.replace(/\.json$/, '.node.png'), fullPage: true });
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
