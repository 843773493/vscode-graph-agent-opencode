import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { parse } from 'jsonc-parser';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const required = name => { if (!process.env[name]) throw new Error('缺少 ' + name); return process.env[name]; };
const run = await TestRunContext.fromTestFile('tests/integration/clients/web/test_settings_editor_target.py').prepare();
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const result = { phase: 'initialize', pageErrors: [], writes: [], preparations: [], hostChannels: [], receipts: [],
  boundary: '正式Workbench、独立真实Gateway和Backend、固定原Node管理组件；页面初始化后经正式安装API生成未激活VSIX，再由configuration_read准备声明并在原设置UI保存。无扩展宿主及模型执行，不代表Relay/SSH/Windows或扩展运行API验收。' };
const pending = [];
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const phase = async value => { result.phase = value; await save(); };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-extensions', '--renderer-process-limit=2'] });
  run.addCleanup('设置 Chromium', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('设置 BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error.stack ?? error)));
  page.on('websocket', socket => { if (/\/extension-(?:web-)?hosts\/channel$/.test(new URL(socket.url()).pathname)) result.hostChannels.push(socket.url()); });
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/gateway/extension-configurations/write') result.writes.push(request.postDataJSON());
  });
  page.on('response', response => {
    if (new URL(response.url()).pathname !== '/api/gateway/extension-configurations/prepare' || response.status() !== 200) return;
    pending.push(response.json().then(value => {
      expect(value.request_id).toBe(response.headers()['x-request-id']);
      result.preparations.push(value.data);
    }));
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
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
  await page.goto(new URL('/api/gateway/health', required('BOXTEAM_BROWSER_BASE_URL')).href);
  await api('/api/gateway/users/guest', 'POST', { tracking: { source: 'settings-editor' } });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  expect(listing.active_workspace.gateway_id).toBe(health.gateway_id);
  // 真实页面初始化还必须完成业务工作区请求，不能只证明静态首页可见。
  await api('/api/v1/workspace');
  await command('workbench.action.openSettings2');
  const editor = page.locator('.settings-editor:visible');
  await expect(editor).toBeVisible();
  await expect(editor.locator('.settings-context-label')).toContainText('写入授权：当前工作台管理');
  await expect(editor.locator('.settings-context-label')).toContainText('打开上下文：当前工作台');
  expect(result.preparations).toHaveLength(0);
  // 在窗口运行清单已初始化后经产品入口安装，设置不能借已运行扩展的贡献补全 schema。
  const installed = await api('/api/gateway/extension-installations/install', 'POST', JSON.parse(required('BOXTEAM_BROWSER_SETTINGS_INSTALL')));
  expect(installed.installed.id).toBe('boxteam-probe.target-inventory');
  await phase('choose-target');
  await editor.getByRole('button', { name: '目标：本地 · 当前工作台', exact: true }).click();
  const picker = page.locator('.quick-input-widget:visible');
  await expect(picker).toBeVisible();
  await picker.locator('.monaco-list-row').filter({ hasText: '本地' }).first().click();
  await expect(editor.locator('.settings-context-label')).toContainText('范围：目标用户', { timeout: 20_000 });
  await expect(editor.locator('.settings-context-label')).toContainText('写入授权：已授权（打开时）');
  await expect(editor.locator('.settings-context-label')).toContainText('打开上下文：未关联工作区');
  await Promise.all(pending);
  expect(result.preparations.length).toBeGreaterThan(0);
  result.binding = result.preparations.at(-1);
  const targetButtonName = '目标：本地 · ' + result.binding.backend_id;
  await expect(editor.getByRole('button', { name: targetButtonName, exact: true })).toBeVisible();
  expect(result.binding.configuration_write_allowed).toBe(true);
  expect(await editor.locator('.settings-context-label').getAttribute('title')).toContain('Gateway ' + result.binding.gateway_id);
  expect(result.binding.extensions.map(extension => extension.extension_id)).toContain('boxteam-probe.target-inventory');
  result.headerSnapshot = { text: await editor.locator('.settings-context-label').innerText(),
    tooltip: await editor.locator('.settings-context-label').getAttribute('title'), bounds: await editor.locator('.settings-context-label').boundingBox() };
  await editor.screenshot({ path: output.replace(/\.json$/, '.header.png') });
  const read = async () => {
    const { protocol_version, gateway_id, backend_id, profile_id, view_id, workspace_ids } = result.binding;
    const value = await api('/api/gateway/extension-configurations/read', 'POST', { protocol_version, gateway_id, backend_id, profile_id, view_id, workspace_ids,
      resource_uri: result.binding.context.remote_settings.resource_uri });
    return parse(Buffer.from(value.content_base64, 'base64').toString('utf8'));
  };
  await phase('independent-schema');
  await command('settings.action.search');
  await page.keyboard.insertText('@ext:boxteam-probe.target-inventory @id:schemaProbe.independent');
  const independent = editor.locator('[data-key="schemaProbe.independent"] .setting-item-control input');
  await expect(independent).toBeVisible({ timeout: 15_000 });
  await expect(independent).toHaveValue('schema-default');
  await independent.fill('schema-saved');
  await command('workbench.action.files.save');
  await expect.poll(read).toEqual({ 'schemaProbe.independent': 'schema-saved' });
  result.independentSchemaSaved = true;
  // 纯过滤与来源切换沿当前设置 Pane，不由测试构造搜索结果或改配置模型。
  const search = async query => {
    await command('settings.action.search');
    await page.keyboard.press('Control+A');
    await page.keyboard.insertText(query);
    await expect(editor.locator('.settings-header .view-lines')).toContainText(query);
  };
  await search('@modified @ext:boxteam-probe.target-inventory');
  await expect(independent).toBeVisible();
  await expect(independent).toHaveValue('schema-saved');
  await editor.getByText('当前工作台 profile', { exact: true }).click();
  await expect(independent).toHaveCount(0);
  await editor.getByText('目标 profile · 本地', { exact: true }).click();
  await expect(independent).toBeVisible();
  await expect(independent).toHaveValue('schema-saved');
  await search('@lang:typescript @id:editor.tabSize');
  const tabSize = editor.locator('[data-key="editor.tabSize"] .setting-item-control input');
  await expect(tabSize).toBeVisible();
  await expect(tabSize).toHaveValue('5');
  result.searchFilters = { modifiedExtension: true, sourceSwitch: true, languageDefault: 5 };
  await command('settings.action.search');
  await page.keyboard.press('Control+A');
  await page.keyboard.insertText('@id:http.proxy');
  await expect(editor.locator('.settings-header .view-lines')).toContainText('@id:http.proxy');
  const value = editor.locator('[data-key="http.proxy"] .setting-item-control input');
  await expect(value).toBeVisible({ timeout: 15_000 });
  const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/extension-configurations/write'
    && Buffer.from(response.request().postDataJSON().content_base64, 'base64').toString('utf8').includes('http://target.invalid:8080'));
  await value.fill('http://target.invalid:8080');
  await editor.getByText('当前工作台 profile', { exact: true }).click();
  await expect(editor.locator('.settings-context-label')).toContainText('写入授权：当前工作台管理');
  const savedResponse = await saved;
  expect(savedResponse.status()).toBe(200);
  const savedReceipt = await savedResponse.json();
  expect(savedReceipt.request_id).toBe(savedResponse.headers()['x-request-id']);
  await expect.poll(async () => (await read())['http.proxy']).toBe('http://target.invalid:8080');
  await editor.getByText('目标 profile · 本地', { exact: true }).click();
  await expect(editor.locator('.settings-context-label')).toContainText('写入授权：已授权（打开时）');
  await expect(value).toHaveValue('http://target.invalid:8080');
  await phase('json-retention');
  await command('settings.switchToJSON');
  const text = page.locator('.editor-instance:visible .monaco-editor .view-lines').last();
  await expect(text).toBeVisible();
  await text.click(); await page.keyboard.press('Control+A');
  await page.keyboard.insertText('// 目标原配置注释\n{"http.proxy":"http://target.invalid:8080","http.proxyStrictSSL":false}\n');
  await command('workbench.action.closeOtherEditors');
  await expect(page.locator('.settings-editor').getByRole('button', { name: targetButtonName, exact: true, includeHidden: true })).toHaveCount(0);
  await command('workbench.action.files.save');
  await expect.poll(read).toEqual({ 'http.proxy': 'http://target.invalid:8080', 'http.proxyStrictSSL': false });
  result.jsonSavedAfterClosingSettingsUI = true;
  await phase('json-to-ui');
  await command('_workbench.openUserSettingsEditor');
  await expect(editor.getByRole('button', { name: targetButtonName, exact: true })).toBeVisible();
  result.jsonReturnedToTargetUI = true;
  // 原ModalEditorPart明确不持久化输入；通过原命令移入主编辑器后验证序列化恢复。
  await command('workbench.action.moveModalEditorToMain');
  await expect(page.locator('.monaco-modal-editor-block')).toHaveCount(0);
  await expect(editor.getByRole('button', { name: targetButtonName, exact: true })).toBeVisible();
  await phase('restore-main-editor');
  // 走原Reload Window生命周期，由产品等待存储提交；不由测试预写或主动flush存储。
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
    page.evaluate(() => { void globalThis.workbench.commands.executeCommand('workbench.action.reloadWindow'); }),
  ]);
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await expect(editor.getByRole('button', { name: targetButtonName, exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(editor.locator('.settings-context-label')).toContainText('范围：目标用户');
  await expect(editor.locator('.settings-context-label')).toContainText('写入授权：已授权（打开时）');
  await expect(editor.locator('.settings-context-label')).toContainText('打开上下文：未关联工作区');
  expect(await editor.locator('.settings-context-label').getAttribute('title')).toContain('保存时会重新校验');
  await expect.poll(read).toEqual({ 'http.proxy': 'http://target.invalid:8080', 'http.proxyStrictSSL': false });
  result.restoredTarget = true;
  await Promise.all(pending);
  await phase('complete');
} catch (error) {
  failure = error;
  result.error = String(error.stack ?? error);
  if (page) await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true }).catch(screenshotError => { result.screenshotError = String(screenshotError); });
} finally {
  await Promise.allSettled(pending);
  await save();
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '设置验收及清理失败') : error; }
}
if (failure) throw failure;
