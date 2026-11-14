import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { planChatCalls } from '../../../support/e2e_control/browser_client.mjs';
import { webSelectors } from '../../../clients/selectors/web-selectors.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_BROWSER_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', consoleErrors: [], consoleWarnings: [], resourceRequests: [], pageErrors: [], writes: [], approvalRequests: [], pluginTargets: [], initialization: [], configurationReads: [], sessionCreates: [], messagePosts: [], activationRequests: [] };
const isHiddenSettingsListWarning = warning => warning.startsWith('Measured item node at 0px- ensure that ListView is not display:none before measuring row height!') &&
  warning.includes('probeDynamicHeights') && warning.includes('refreshTree') && warning.includes('onConfigUpdate');
// 驱动进程异常退出时仍保留已完成阶段，避免把工具崩溃误判为页面行为。
const checkpoint = async phase => {
  result.phase = phase;
  await writeFile(path.join(artifacts, 'progress.json'), JSON.stringify(result, null, 2));
};
let browser, page;
const started = performance.now();
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1100, height: 884 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error') result.consoleErrors.push(message.text());
    if (message.type() === 'warning') result.consoleWarnings.push(message.text());
  });
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/service-plugins/inventory')) result.pluginTargets.push(request.postDataJSON());
    if (request.url().includes('/tool-approvals')) result.approvalRequests.push(request.url());
    if (new URL(request.url()).pathname === '/api/v1/resources') result.resourceRequests.push(request.url());
    if (request.method() === 'PUT' && request.url().includes('/config/sources/')) result.writes.push(request.postDataJSON());
    if (request.method() === 'GET' && request.url().includes('/config/sources/')) result.configurationReads.push({ url: request.url(), gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/sessions') result.sessionCreates.push(request.postDataJSON());
    if (request.method() === 'POST' && /\/api\/v1\/sessions\/[^/]+\/threads\/[^/]+\/messages$/.test(new URL(request.url()).pathname)) result.messagePosts.push({ url: request.url(), body: request.postDataJSON(), gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    if (request.method() === 'POST' && /\/api\/gateway\/workspaces\/[^/]+\/activate$/.test(new URL(request.url()).pathname)) result.activationRequests.push(request.url());
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const openHistoryCacheInformation = () => page.evaluate(({ id }) => {
    void globalThis.workbench.commands.executeCommand(id);
  }, { id: 'boxteam.thread.inspectHistoryCache' });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  result.workbenchReadyMs = performance.now() - started;
  result.initialization = await page.evaluate(async owner => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    return Promise.all(['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace'].map(async url => {
      const response = await fetch(url, { headers: { 'X-Local-Token': credential.data.token,
        'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } });
      return { status: response.status, requestId: response.headers.get('X-Request-ID'), body: await response.json() };
    }));
  }, fixture);
  for (const response of result.initialization) {
    assert.equal(response.status, 200); assert(response.requestId); assert.equal(response.requestId, response.body.request_id);
  }
  const activeWorkspace = result.initialization[1].body.data.active_workspace.workspace_id;
  await checkpoint('disabled-terminal');
  await command('boxteam.workspace.terminal.focus');
  await expect(page.locator('.native-workspace-panel[data-panel-kind="terminal"]')).toContainText('未绑定');
  // 越过两次原轮询周期，未绑定不能向后端反复发出必然失败的资源请求。
  await page.waitForTimeout(4500);
  assert.deepEqual(result.resourceRequests, []);
  await command('workbench.action.closePanel');
  await checkpoint('extensions-sidebar');
  await command('workbench.view.extensions');
  const sidebar = page.locator('.service-plugin-packages-view');
  const row = sidebar.locator('[data-plugin-runtime-row][data-package-id="proof.management"][data-plugin-id="workspace-probe"]');
  await expect(row).toBeVisible();
  result.sidebarReadyMs = performance.now() - started;
  await expect(page.locator('.quick-input-widget')).not.toBeVisible();
  const workspaceSelector = sidebar.getByRole('combobox', { name: '插件管理工作区', exact: true });
  await expect(workspaceSelector).toHaveValue(result.initialization[1].body.data.items.find(item => item.workspace_id === fixture.workspaceId).name);
  const selectors = sidebar.locator('.service-plugin-scope .monaco-action-bar .action-item.select-container select');
  await expect(selectors).toHaveCount(2);
  result.targetToolbar = await selectors.evaluateAll(elements => elements.map(element => {
    const style = getComputedStyle(element);
    return { height: element.getBoundingClientRect().height, paddingLeft: style.paddingLeft, paddingRight: style.paddingRight,
      borderStyle: style.borderStyle, appearance: style.appearance };
  }));
  for (const selector of result.targetToolbar) {
    assert(selector.height >= 20, JSON.stringify(selector));
    assert.equal(selector.paddingLeft, '8px');
    assert.equal(selector.paddingRight, '23px');
    assert.equal(selector.appearance, 'none');
    assert.equal(selector.borderStyle, 'solid');
  }
  await workspaceSelector.click();
  await expect(page.locator('.monaco-select-box-dropdown-container.visible')).toBeVisible();
  await expect(page.locator('.monaco-select-box-dropdown-container.visible .select-box-details-pane')).not.toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('.monaco-select-box-dropdown-container.visible')).not.toBeVisible();
  const detailStarted = performance.now();
  await row.locator('.name').click();
  const details = page.locator('.service-plugin-editor[data-plugin-id="workspace-probe"]');
  await expect(details).toBeVisible();
  await expect(details.locator('.service-plugin-detail-actions .action-label').first()).toBeVisible();
  result.detailReadyMs = performance.now() - detailStarted;
  assert(result.detailReadyMs <= 3000, `插件详情可用耗时 ${result.detailReadyMs}ms`);
  assert.equal(await sidebar.locator('.service-plugin-editor').count(), 0);
  await expect(sidebar).toBeVisible();
  const nativeLayout = await details.evaluate(element => {
    const header = element.querySelector(':scope > .header');
    return { display: getComputedStyle(header).display,
      iconSize: getComputedStyle(header.querySelector('.extension-icon .codicon')).fontSize,
      headingSize: getComputedStyle(header.querySelector('.title > .name')).fontSize,
      buttons: [...header.querySelectorAll('.extension-action')].map(button => getComputedStyle(button).backgroundColor) };
  });
  assert.equal(nativeLayout.display, 'flex');
  assert.equal(nativeLayout.iconSize, '128px');
  assert.equal(nativeLayout.headingSize, '26px');
  assert(nativeLayout.buttons.length >= 2 && nativeLayout.buttons[0] !== nativeLayout.buttons[1], JSON.stringify(nativeLayout));
  await expect(row).toHaveClass('extension-list-item');
  assert.equal((await row.boundingBox()).height, 72);
  await expect(details.getByLabel('参数 schema')).not.toBeVisible();
  await details.locator('.navbar').getByRole('button', { name: '功能贡献', exact: true }).click();
  await expect(details.getByLabel('参数 schema')).toBeVisible();
  await details.locator('.navbar').getByRole('button', { name: '详细信息', exact: true }).click();
  await page.mouse.move(10, 400);
  await page.screenshot({ path: path.join(artifacts, 'management-wide.png') });
  result.nativeLayout = nativeLayout;
  await page.locator('.tabs-container .tab.active').dblclick();
  await command('workbench.action.splitEditorRight');
  await expect(page.locator('.service-plugin-editor[data-plugin-id="workspace-probe"]')).toHaveCount(2);
  await command('workbench.action.closeActiveEditor');
  await checkpoint('sidebar-enable-disable');
  const quick = page.locator('.quick-input-widget');
  const toggle = async label => {
    await row.hover();
    await row.getByRole('button', { name: '管理 工作区探针', exact: true }).click();
    await page.getByRole('menuitem', { name: label, exact: true }).click({ delay: 150 });
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: 'workspace_root_mutable_override' }).first().click();
    await quick.locator('.monaco-list-row').filter({ hasText: '确认保存工作区插件绑定' }).first().click();
  };
  await toggle('启用此范围');
  await expect(row.locator('[data-plugin-runtime-value]')).toContainText('启用');
  await toggle('停用此范围');
  await expect(row.locator('[data-plugin-runtime-value]')).toContainText('停用');
  await checkpoint('gateway-scope-fixed-editor');
  await sidebar.getByRole('button', { name: '插件管理范围及安装', exact: true }).click();
  await page.getByRole('menuitem', { name: '当前 Gateway', exact: true }).click({ delay: 150 });
  await expect(workspaceSelector).toBeDisabled();
  await expect(sidebar.locator('[data-plugin-id="gateway-probe"]')).toBeVisible();
  await expect(page.locator('.service-plugin-editor[data-plugin-id="workspace-probe"]')).toHaveAttribute('data-workspace-id', fixture.workspaceId);
  await sidebar.getByRole('button', { name: '插件管理范围及安装', exact: true }).click();
  await page.getByRole('menuitem', { name: '以后新建工作区', exact: true }).click({ delay: 150 });
  await expect(sidebar.locator('[data-plugin-id="workspace-probe"]')).toBeVisible();
  await sidebar.getByRole('button', { name: '插件管理范围及安装', exact: true }).click();
  await page.getByRole('menuitem', { name: '当前 Gateway', exact: true }).click({ delay: 150 });
  await expect(sidebar.locator('[data-plugin-id="gateway-probe"]')).toBeVisible();
  await checkpoint('narrow-native-controls');
  await page.setViewportSize({ width: 685, height: 884 });
  await expect(sidebar).toBeVisible();
  await expect(page.locator('.service-plugin-editor')).toBeVisible();
  const boxes = await Promise.all([sidebar.boundingBox(), page.locator('.service-plugin-editor').boundingBox()]);
  assert(boxes[0].x + boxes[0].width <= boxes[1].x + 2, JSON.stringify(boxes));
  await page.mouse.move(10, 400);
  await page.screenshot({ path: path.join(artifacts, 'management-narrow.png') });
  await checkpoint('reload-editor');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('.service-plugin-editor[data-plugin-id="workspace-probe"]')).toBeVisible({ timeout: 30000 });
  await expect(page.locator('.service-plugin-editor')).toHaveAttribute('data-workspace-id', fixture.workspaceId);
  await checkpoint('native-manage-search-settings');
  await page.getByRole('button', { name: '管理', exact: true }).click();
  await expect(page.getByRole('menuitem', { name: /设置/ }).first()).toBeVisible();
  const appearanceMenuItem = page.getByRole('menuitem', { name: 'BoxTeam 外观', exact: true });
  await expect(appearanceMenuItem).toBeVisible();
  await appearanceMenuItem.click();
  await expect(page.locator('[data-key="boxteam.appearance.background"]')).toBeVisible();
  result.appearanceManagement = { menuEntry: 'BoxTeam 外观', categoryVisible: true };
  await command('workbench.action.closeActiveEditor');
  await page.keyboard.press('Escape');
  await page.keyboard.press('Control+Shift+f');
  await expect(page.locator('.search-view')).toBeVisible();
  await command('workbench.action.openSettings');
  await checkpoint('settings-editor-open');
  await expect(page.locator('.settings-context-label').getByRole('link', { name: '模型与 Provider', exact: true })).toBeVisible();
  await page.locator('.settings-context-label').getByRole('link', { name: '模型与 Provider', exact: true }).click();
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: 'workspace_root_mutable_override' }).first().click();
  await expect(quick).not.toBeVisible();
  await expect(page.locator('.configuration-entities[data-definition-kind="providers"]')).toBeVisible();
  await expect(page.locator('.configuration-entities').getByRole('button', { name: '新增 Provider', exact: true })).toBeVisible();
  await command('boxteam.appearance.open');
  await expect(page.locator('[data-key="boxteam.appearance.background"]')).toBeVisible();
  result.appearanceManagement = { ...result.appearanceManagement, command: 'boxteam.appearance.open' };
  await command('workbench.action.closeActiveEditor');
  await command('workbench.action.openGlobalKeybindings');
  await expect(page.locator('.keybindings-editor')).toBeVisible();
  result.keyboardShortcuts = { loaded: true };
  await command('workbench.action.closeActiveEditor');
  await command('workbench.profiles.actions.manageProfiles');
  await expect(page.locator('.profiles-editor')).toBeVisible();
  result.profiles = { loaded: true };
  await command('workbench.action.closeActiveEditor');
  const registered = await page.evaluate(async () => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    return (await (await fetch('/api/gateway/workspaces', { headers: { 'X-Local-Token': credential.data.token } })).json()).data;
  });
  assert.equal(registered.active_workspace.workspace_id, activeWorkspace);
  await checkpoint('native-language-detection');
  await command('workbench.action.files.newUntitledFile');
  await command('type', { text: 'import pathlib\n\ndef list_sources(root: pathlib.Path) -> list[str]:\n    """List Python sources in a directory."""\n    return [str(path) for path in root.rglob("*.py")]\n\nif __name__ == "__main__":\n    print(list_sources(pathlib.Path.cwd()))\n' });
  await command('editor.detectLanguage');
  await expect(page.locator('[id="status.editor.mode"]')).toContainText('Python');
  await command('workbench.action.revertAndCloseActiveEditor');
  result.warmApiSamples = await page.evaluate(async owner => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const samples = [];
    for (const endpoint of ['/api/gateway/service-plugins/inventory', '/api/v1/config/sources/workspace_root_mutable_override']) {
      const rows = [];
      for (let index = 0; index < 20; index++) {
        const start = performance.now();
        const response = await fetch(endpoint, { method: endpoint.includes('/inventory') ? 'POST' : 'GET',
          headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token,
            'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId },
          ...(endpoint.includes('/inventory') ? { body: JSON.stringify({ gateway_id: owner.gatewayId }) } : {}) });
        const text = await response.text(), body = JSON.parse(text);
        if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(text);
        rows.push({ ms: performance.now() - start, bytes: new TextEncoder().encode(text).length, requestId: body.request_id });
      }
      const sorted = rows.map(row => row.ms).sort((a, b) => a - b);
      samples.push({ endpoint, rows, p50: sorted[9], p95: sorted[18], max: sorted[19] });
    }
    return samples;
  }, fixture);
  // 暖样本先完整采集，性能失败不能挡住本轮后续业务行为观察；预算在全部功能断言后统一判定。
  await checkpoint('welcome-entry-and-multi-thread-management');
  const localToken = await page.evaluate(async () => (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token);
  const ownerHeaders = { 'X-Local-Token': localToken, 'X-BoxTeam-Target-Gateway-Id': fixture.gatewayId,
    'X-BoxTeam-Workspace-Id': fixture.workspaceId, 'Content-Type': 'application/json' };
  const api = async (pathname, method = 'GET', body) => page.evaluate(async ({ pathname, method, body, headers }) => {
    const response = await fetch(pathname, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const text = await response.text();
    if (!response.ok) throw new Error(`API ${response.status}: ${text}`);
    const value = JSON.parse(text);
    if (value.request_id !== response.headers.get('X-Request-ID')) throw new Error(`request_id不一致：${pathname}`);
    return value;
  }, { pathname, method, body, headers: ownerHeaders });
  const getActiveWorkspace = async () => page.evaluate(async token => {
    const response = await fetch('/api/gateway/workspaces', { headers: { 'X-Local-Token': token } });
    if (!response.ok) throw new Error(await response.text());
    return (await response.json()).data.active_workspace.workspace_id;
  }, localToken);
  const readSessionIds = async () => (await api('/api/v1/sessions?limit=200')).data.items.map(session => session.session_id);
  assert.deepEqual(await readSessionIds(), [], 'Welcome入口场景开始时必须没有Session');
  const activeBeforeWelcome = await getActiveWorkspace();
  const welcomeEntries = page.locator('.gettingStarted .start-container');
  const openWelcome = async () => {
    await command('_workbench.open', 'walkThrough://vscode_getting_started_page', [undefined, { pinned: true, showWelcome: true }]);
    await expect(welcomeEntries).toBeVisible();
  };
  const chooseWorkspaceSource = async () => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: 'workspace_root_mutable_override' }).first().click();
    await expect(quick).not.toBeVisible();
  };
  const managementInventoryStart = result.pluginTargets.length, approvalStart = result.approvalRequests.length;
  await openWelcome();
  await welcomeEntries.locator('[x-dispatch="selectStartEntry:boxteam.manageProviders"]').click();
  await chooseWorkspaceSource();
  const chineseProviders = page.locator('.configuration-entities[data-definition-kind="providers"]');
  await expect(chineseProviders).toBeVisible();
  await expect(chineseProviders.getByRole('button', { name: '新增 Provider', exact: true })).toBeVisible();
  await command('workbench.action.closeActiveEditor');
  await openWelcome();
  await welcomeEntries.locator('[x-dispatch="selectStartEntry:boxteam.manageAgents"]').click();
  await chooseWorkspaceSource();
  await expect(page.locator('.configuration-entities[data-definition-kind="agents"]')).toBeVisible();
  await command('workbench.action.closeActiveEditor');
  await openWelcome();
  await welcomeEntries.locator('[x-dispatch="selectStartEntry:boxteam.manageGateway"]').click();
  const gatewayDialog = page.locator('.native-navigation-dialog');
  await expect(gatewayDialog.locator('.gateway-control-shell')).toBeVisible();
  await gatewayDialog.locator('button.sidebar-icon-button').click();
  await expect(gatewayDialog).toHaveCount(0);
  assert.deepEqual(await readSessionIds(), [], 'Provider／Agent／Gateway Welcome入口不能创建Session');
  assert.equal(await getActiveWorkspace(), activeBeforeWelcome, 'Welcome管理入口不能激活或切换Workspace');
  assert.deepEqual(result.activationRequests, []);
  await checkpoint('welcome-management-verified');
  assert(result.configurationReads.length >= 2);
  assert(result.configurationReads.every(read => read.gatewayId === fixture.gatewayId &&
    (read.url.includes('/api/gateway/config/sources/') ? read.workspaceId === undefined : read.workspaceId === fixture.workspaceId)), JSON.stringify(result.configurationReads));
  assert.equal(result.pluginTargets.length, managementInventoryStart, 'Welcome管理入口不能触发全目标插件清点');
  assert.equal(result.approvalRequests.length, approvalStart, 'Welcome管理入口不能新增工具审批轮询');
  result.welcomeManagement = { entries: ['boxteam.manageProviders', 'boxteam.manageAgents', 'boxteam.manageGateway'],
    noSession: true, activeWorkspacePreserved: true, owner: { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId } };
  await page.evaluate(() => localStorage.setItem('vscode.nls.locale', 'en'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await command('workbench.view.explorer');
  await expect(page.locator('.native-session-navigation')).toBeVisible();
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`)).toBeVisible({ timeout: 30000 });
  const openEnglishProviders = command('boxteam.settings.modelsProviders');
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: 'workspace_root_mutable_override' }).first().click();
  await expect(page.locator('.editor-instance:visible .monaco-editor')).toBeVisible();
  await openEnglishProviders;
  const englishProviders = page.locator('.configuration-entities[data-definition-kind="providers"]');
  await expect(englishProviders.getByRole('button', { name: 'Add Provider', exact: true })).toBeVisible();
  result.providerManagementEnglish = true;
  await command('workbench.action.closeActiveEditor');
  await command('boxteam.appearance.open');
  await expect(page.locator('[data-key="boxteam.appearance.background"]')).toBeVisible();
  result.appearanceManagementEnglish = true;
  await command('workbench.action.closeActiveEditor');
  await page.evaluate(() => localStorage.setItem('vscode.nls.locale', 'zh-cn'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  await command('workbench.view.explorer');
  await expect(page.locator('.native-session-navigation')).toBeVisible();
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`)).toBeVisible({ timeout: 30000 });
  result.bilingualProviderManagement = { chinese: true, english: true };
  await command('workbench.action.closeAllEditors');
  await page.setViewportSize({ width: 1920, height: 1080 });
  const threads = [];
  for (let index = 0; index < 6; index++) {
    const session = (await api('/api/v1/sessions', 'POST', { title: `并行管理Thread ${index + 1}`, agent_id: 'default' })).data;
    const mainThread = (await api(`/api/v1/sessions/${session.session_id}/main-thread`)).data;
    threads.push({ sessionId: session.session_id, threadId: mainThread.thread_id });
  }
  for (let index = 0; index < threads.length; index++) {
    if (index > 0) {
      await command('workbench.action.splitEditorRight');
      await command('workbench.action.focusLastEditorGroup');
      await expect(page.locator('.editor-group-container')).toHaveCount(index + 1);
    }
    await command('_workbench.open', `boxteam-thread://${fixture.gatewayId}/${fixture.workspaceId}/${threads[index].sessionId}/${threads[index].threadId}`, [undefined, { pinned: true }]);
  }
  const editorGroups = page.locator('.editor-group-container');
  await expect(editorGroups).toHaveCount(6);
  const threadPanes = page.locator(`section.boxteam-thread[data-workspace-id="${fixture.workspaceId}"]`);
  await expect(threadPanes).toHaveCount(6);
  for (const thread of threads) {
    await expect(page.locator(`section.boxteam-thread[data-session-id="${thread.sessionId}"][data-thread-id="${thread.threadId}"]`).getByTestId('composer-input')).toBeVisible();
  }
  const activeBeforeProviderManagement = await getActiveWorkspace();
  const providerPluginInventoryStart = result.pluginTargets.length;
  const preservedComposer = page.locator(`section.boxteam-thread[data-session-id="${threads[5].sessionId}"][data-thread-id="${threads[5].threadId}"]`).getByTestId('composer-input');
  const unsentDraft = '管理详情打开和关闭期间保留的未发送草稿。';
  await preservedComposer.fill(unsentDraft);
  const editorTabLabels = () => page.locator('.tabs-container .tab').evaluateAll(tabs => tabs
    .map(tab => `${tab.textContent ?? ''} ${tab.getAttribute('title') ?? ''} ${tab.getAttribute('aria-label') ?? ''}`));
  const originalThreadTabLabels = await editorTabLabels();
  assert.equal(threads.filter(thread => originalThreadTabLabels.some(label => label.includes(thread.threadId))).length, 6,
    JSON.stringify(originalThreadTabLabels));
  const providerReadStart = result.configurationReads.length;
  const openProviders = command('boxteam.settings.modelsProviders');
  await chooseWorkspaceSource();
  await openProviders;
  const providerEditor = page.locator('.configuration-entities[data-definition-kind="providers"]');
  await expect(providerEditor).toBeVisible();
  await expect(providerEditor.getByRole('button', { name: '新增 Provider', exact: true })).toBeVisible();
  const providerReads = result.configurationReads.slice(providerReadStart);
  assert(providerReads.length > 0, 'Provider 页面必须通过正式配置 owner 读取来源');
  assert(providerReads.every(read => read.gatewayId === fixture.gatewayId && read.workspaceId === fixture.workspaceId), JSON.stringify(providerReads));
  await expect(threadPanes).toHaveCount(5);
  await expect(editorGroups).toHaveCount(6);
  const threadTabLabelsDuringManagement = await editorTabLabels();
  assert.equal(threads.filter(thread => threadTabLabelsDuringManagement.some(label => label.includes(thread.threadId))).length, 6,
    JSON.stringify(threadTabLabelsDuringManagement));
  await command('workbench.action.closeActiveEditor');
  await expect(providerEditor).toHaveCount(0);
  await expect(threadPanes).toHaveCount(6);
  await expect(preservedComposer).toHaveValue(unsentDraft);
  assert.equal(await getActiveWorkspace(), activeBeforeProviderManagement, 'Provider 管理目标不能改变导航活动 Workspace');
  assert.deepEqual(result.activationRequests, [], 'Provider 管理页不能激活工作区');
  assert.equal(result.pluginTargets.length, providerPluginInventoryStart, 'Provider 管理页不能触发全目标插件清点');
  result.providerManagementInSixThreads = { loaded: true, closed: true, threadCountPreserved: 6,
    visibleThreadPanesDuringManagement: 5,
    defaultGatewayId: providerReads[0].gatewayId, workspaceId: providerReads[0].workspaceId,
    activeWorkspacePreserved: true, noActivation: true };
  const threadApprovalStart = result.approvalRequests.length;
  await command('workbench.view.extensions');
  await expect(page.locator('.service-plugin-packages-view')).toBeVisible();
  await expect(threadPanes).toHaveCount(6);
  await command('workbench.action.closeSidebar');
  await expect(page.locator('.service-plugin-packages-view')).toHaveCount(0);
  await expect(threadPanes).toHaveCount(6);
  await expect(preservedComposer).toHaveValue(unsentDraft);
  const pluginInventoryStart = result.pluginTargets.length;
  await checkpoint('six-threads-open');
  const composerThread = page.locator(`section.boxteam-thread[data-session-id="${threads[5].sessionId}"][data-thread-id="${threads[5].threadId}"]`);
  const responseText = '管理页关闭后，Composer仍能继续工作。';
  await planChatCalls(threads[5].sessionId, threads[5].threadId, [{ label: 'management-after-close', text: responseText }], 'mcp-plugin-model');
  await composerThread.locator(webSelectors.composer).fill('请确认Composer在管理页关闭后仍然可用。');
  await composerThread.locator(webSelectors.sendButton).click();
  await expect(composerThread.getByText(responseText, { exact: true })).toBeVisible({ timeout: 60000 });
  const otherThread = page.locator(`section.boxteam-thread[data-session-id="${threads[4].sessionId}"][data-thread-id="${threads[4].threadId}"]`);
  const otherResponseText = '另一Thread的后端历史和展示缓存应保持独立。';
  await planChatCalls(threads[4].sessionId, threads[4].threadId, [{ label: 'management-other-thread', text: otherResponseText }], 'mcp-plugin-model');
  await otherThread.locator(webSelectors.composer).fill('请验证另一条 Thread 的状态保持独立。');
  await otherThread.locator(webSelectors.sendButton).click();
  await expect(otherThread.getByText(otherResponseText, { exact: true })).toBeVisible({ timeout: 60000 });
  await checkpoint('six-thread-composer-replied');
  const cacheRecord = owner => page.evaluate(async ({ gatewayId, workspaceId, sessionId, threadId }) => {
    const database = await new Promise((resolve, reject) => {
      const request = indexedDB.open('boxteam-browser-views', 1);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
    const indexKey = JSON.stringify([gatewayId, 'thread-history-v1']);
    const snapshotKey = JSON.stringify([gatewayId, 'thread-history', workspaceId, sessionId, threadId]);
    const transaction = database.transaction('views', 'readonly');
    const store = transaction.objectStore('views');
    const indexRequest = store.get(indexKey), snapshotRequest = store.get(snapshotKey);
    return new Promise((resolve, reject) => {
      transaction.oncomplete = () => {
        database.close();
        resolve({ indexed: indexRequest.result?.[snapshotKey] ?? null, snapshot: snapshotRequest.result ?? null });
      };
      transaction.onabort = () => { database.close(); reject(transaction.error); };
    });
  }, { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId, ...owner });
  const cacheOwner = thread => ({ sessionId: thread.sessionId, threadId: thread.threadId });
  await expect.poll(async () => (await cacheRecord(cacheOwner(threads[5]))).indexed, { timeout: 10000 }).toBeTruthy();
  await expect.poll(async () => (await cacheRecord(cacheOwner(threads[4]))).indexed, { timeout: 10000 }).toBeTruthy();
  const inspectedThread = threads[5], preservedThread = threads[4];
  await command('workbench.action.focusLastEditorGroup');
  await expect(composerThread.getByTestId('composer-input')).toBeVisible();
  await openHistoryCacheInformation();
  const cacheInfo = page.locator('.monaco-dialog-box[role="dialog"]');
  await expect(cacheInfo).toContainText(fixture.gatewayId);
  await expect(cacheInfo).toContainText(fixture.workspaceId);
  await expect(cacheInfo).toContainText(inspectedThread.sessionId);
  await expect(cacheInfo).toContainText(inspectedThread.threadId);
  await expect(cacheInfo).toContainText('已保存的展示镜像：');
  await expect(cacheInfo).toContainText('没有等待保存的镜像');
  const beforeClear = await cacheRecord(cacheOwner(inspectedThread));
  assert.equal(beforeClear.indexed?.bytes, beforeClear.snapshot?.bytes);
  assert.equal(beforeClear.snapshot?.owner.gatewayId, fixture.gatewayId);
  assert.equal(beforeClear.snapshot?.owner.workspaceId, fixture.workspaceId);
  assert.equal(beforeClear.snapshot?.owner.sessionId, inspectedThread.sessionId);
  assert.equal(beforeClear.snapshot?.owner.threadId, inspectedThread.threadId);
  await checkpoint('history-cache-information-verified');
  await cacheInfo.getByRole('button', { name: '清除展示缓存', exact: true }).click();
  const confirmation = page.locator('.monaco-dialog-box[role="dialog"]');
  await expect(confirmation).toContainText(inspectedThread.sessionId);
  await expect(confirmation).toContainText(inspectedThread.threadId);
  await command('workbench.action.focusFirstEditorGroup');
  const firstThread = page.locator(`section.boxteam-thread[data-session-id="${threads[0].sessionId}"][data-thread-id="${threads[0].threadId}"]`);
  await expect(firstThread.getByTestId('composer-input')).toBeVisible();
  await confirmation.getByRole('button', { name: '清除展示缓存', exact: true }).click();
  await expect(confirmation).toBeHidden();
  await expect(firstThread.getByTestId('composer-input')).toBeVisible();
  await expect(otherThread.getByTestId('composer-input')).toBeVisible();
  const afterClear = await cacheRecord(cacheOwner(inspectedThread));
  const preservedCache = await cacheRecord(cacheOwner(preservedThread));
  assert.equal(afterClear.indexed, null, '清理确认必须只删除发起时 Thread 的历史缓存索引');
  assert.equal(afterClear.snapshot, null, '清理确认必须删除发起时 Thread 的展示快照');
  assert.equal(preservedCache.indexed?.bytes, preservedCache.snapshot?.bytes, '焦点切换不能清理其它 Thread 缓存');
  const originalHistory = await api(`/api/v1/sessions/${inspectedThread.sessionId}/threads/${inspectedThread.threadId}/history`, 'POST', {
    direction: 'tail', turns: 8, include: ['user', 'text', 'reasoning_detail', 'tool_call', 'tool_result', 'final_response', 'internal'],
  });
  const preservedHistory = await api(`/api/v1/sessions/${preservedThread.sessionId}/threads/${preservedThread.threadId}/history`, 'POST', {
    direction: 'tail', turns: 8, include: ['user', 'text', 'reasoning_detail', 'tool_call', 'tool_result', 'final_response', 'internal'],
  });
  assert(JSON.stringify(originalHistory.data.items).includes(responseText), '清理展示缓存后原 Thread 后端消息历史必须仍存在');
  assert(JSON.stringify(preservedHistory.data.items).includes(otherResponseText), '焦点切换不能更改其它 Thread 后端记录');
  await expect(composerThread.getByTestId('composer-input')).toBeVisible();
  await firstThread.getByTestId('composer-input').fill('缓存清理后其他 Thread Composer 仍能输入。');
  await expect(firstThread.getByTestId('composer-input')).toHaveValue('缓存清理后其他 Thread Composer 仍能输入。');
  await checkpoint('history-cache-owner-stable-after-focus-change');
  assert.equal(result.sessionCreates.length, 6);
  assert.deepEqual(result.sessionCreates.map(item => item.agent_id), Array(6).fill('default'));
  assert.deepEqual(result.messagePosts.map(item => ({ gatewayId: item.gatewayId, workspaceId: item.workspaceId })), Array(2).fill({
    gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId,
  }));
  assert.equal(result.pluginTargets.length, pluginInventoryStart, '多Thread管理打开／关闭不能触发全目标插件清点');
  assert.equal(result.approvalRequests.length, threadApprovalStart, '多Thread管理打开／关闭不能新增工具审批轮询');
  result.multiThreadManagement = { threadCount: 6, editorGroups: 6, managementOpenedAndClosed: true,
    composerContinued: true, unsentDraftPreserved: true,
    modelStreamResponses: 2, cacheInformationRead: true, cacheOwnerStableAfterFocusChange: true };
  for (const target of result.pluginTargets) {
    assert.equal(target.gateway_id, fixture.gatewayId);
    assert(Object.keys(target).every(key => ['gateway_id', 'gateway_connection_id'].includes(key)), JSON.stringify(target));
  }
  assert.equal(result.writes.length, 2);
  assert.equal(result.pageErrors.length, 0);
  result.expectedHiddenSettingsWarnings = result.consoleWarnings.filter(isHiddenSettingsListWarning).length;
  assert.deepEqual(result.consoleWarnings.filter(warning => !isHiddenSettingsListWarning(warning)), []);
  assert.deepEqual(result.consoleErrors, []);
  result.performanceBudgetFailures = result.warmApiSamples.flatMap(sample => [
    ...(!sample.rows.every(row => row.bytes <= 512 * 1024) ? [`${sample.endpoint}: 单条响应超过512KiB`] : []),
    ...(sample.p95 > 500 || sample.max > 1500 ? [`${sample.endpoint}: p95=${sample.p95}ms max=${sample.max}ms`] : []),
  ]);
  assert.deepEqual(result.performanceBudgetFailures, [], '暖 API 样本未满足 D8 预算');
  result.phase = 'completed';
} catch (error) {
  result.error = String(error?.stack ?? error);
  if (page) { await page.screenshot({ path: path.join(artifacts, 'failure.png') }); result.body = await page.locator('body').innerText(); }
  process.exitCode = 1;
} finally {
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  await browser?.close();
}
