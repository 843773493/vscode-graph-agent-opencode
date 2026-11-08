import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { parse } from 'jsonc-parser';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_WEB_CONFIGURATION_SCOPES);
const output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
assert(fixture.gateway_id && fixture.backend_id && fixture.workspace_ids.length === 2 && output,
  '必须显式提供双根目标与正式产物目录');
const run = await TestRunContext.fromTestFile('tests/integration/clients/web/test_settings_editor_target.py').prepare();
const result = { phase: 'initialize', pageErrors: [], consoleErrors: [], nodeChannels: [], bindings: [],
  receipts: [], writes: [], snapshots: [], rejectedResources: [],
  boundary: '正式 Workbench、真实 Gateway 及同 Backend 双根；原 API 安装纯 browser 扩展，经原挂接命令激活真实 Web Worker。配置更新经公开 getConfiguration/update、原 renderer writer 和目标 Python 文件服务；外改仅在产品实际写出稳定 .code-workspace 后模拟。无模型调用，不代替 Node、Relay、Windows、Electron 或完整 dirty/etag/并发验收。' };
const pending = [];
let page, failure;
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const phase = async value => { result.phase = value; await save(); };
const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
const read = (options = {}) => command('boxteam.webScopes.read', options);
const update = options => command('boxteam.webScopes.update', options);
async function snapshot(name) {
  const first = await read({ folder: 0 }), second = await read({ folder: 1 }),
    language = await read({ folder: 0, language: 'typescript' });
  const value = { name, first, second, language };
  result.snapshots.push(value); await save();
  return value;
}
async function api(path, method = 'GET', payload) {
  const receipt = await page.evaluate(async ({ path, method, payload }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token },
      ...(payload ? { body: JSON.stringify(payload) } : {}) });
    return { path, status: response.status, requestId: response.headers.get('X-Request-ID'), body: await response.json() };
  }, { path, method, payload });
  result.receipts.push(receipt);
  assert.equal(receipt.status, 200); assert(receipt.body.request_id); assert.equal(receipt.body.request_id, receipt.requestId);
  return receipt.body.data;
}
async function ready() {
  await expect(page.locator('.monaco-workbench')).toBeVisible({ timeout: 30_000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand, null, { timeout: 30_000 });
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--renderer-process-limit=2'] });
  run.addCleanup('配置作用域 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1700, height: 1100 } });
  run.addCleanup('配置作用域 BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error.stack ?? error)));
  page.on('console', entry => { if (entry.type() === 'error') result.consoleErrors.push(entry.text()); });
  page.on('websocket', socket => {
    const path = new URL(socket.url()).pathname;
    if (path === '/api/gateway/extension-hosts/channel') result.nodeChannels.push(path);
    if (path !== '/api/gateway/extension-web-hosts/channel') return;
    socket.on('framereceived', ({ payload }) => {
      if (typeof payload !== 'string') return;
      const binding = JSON.parse(payload);
      if (binding.context) result.bindings.push(binding);
    });
  });
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (!path.startsWith('/api/gateway/extension-configurations/') || path.endsWith('/events')) return;
    pending.push(response.json().then(body => {
      const receipt = { path, status: response.status(), requestId: response.headers()['x-request-id'],
        request: response.request().postDataJSON(), body };
      assert(body.request_id); assert.equal(receipt.requestId, body.request_id);
      result.receipts.push(receipt);
      if (path.endsWith('/write')) result.writes.push(receipt);
    }));
  });
  const base = process.env.BOXTEAM_BROWSER_BASE_URL;
  await page.goto(new URL('/api/gateway/health', base).href);
  await api('/api/gateway/users/guest', 'POST', { tracking: { source: 'web-configuration-scopes' } });
  await page.goto(base, { waitUntil: 'domcontentloaded' }); await ready();
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  assert.equal(health.gateway_id, fixture.gateway_id);
  assert.equal(workspaces.active_workspace.gateway_id, fixture.gateway_id);
  assert.equal(workspaces.active_workspace.workspace_id, fixture.workspace_ids[0]);
  await api('/api/v1/workspace');

  await phase('attach-original-multiroot-view');
  const navigation = page.waitForURL(url => url.searchParams.has('workspace'), { waitUntil: 'domcontentloaded', timeout: 30_000 });
  const attaching = command('boxteam.extensions.attachWorkspaceViews');
  const picker = page.locator('.quick-input-widget:visible');
  await expect(picker).toBeVisible();
  const rows = picker.locator('.monaco-list-row').filter({ hasText: fixture.gateway_id });
  await expect(rows).toHaveCount(2);
  for (let index = 0; index < 2; index++) await rows.nth(index).locator('.monaco-checkbox').click();
  await picker.locator('input').press('Enter');
  await attaching.catch(error => { if (!String(error).includes('Execution context was destroyed')) throw error; });
  await navigation; await ready();
  await expect.poll(async () => {
    try { return (await read()).worker; }
    catch (error) { if (!String(error).includes("command 'boxteam.webScopes.read' not found")) throw error; }
  }, { timeout: 30_000 }).toBe(true);
  result.binding = result.bindings.find(binding => binding.workspace_ids.length === 2);
  assert(result.binding); assert.equal(result.binding.gateway_id, fixture.gateway_id);
  assert.equal(result.binding.backend_id, fixture.backend_id);
  assert.deepEqual(result.binding.workspace_ids, fixture.workspace_ids);
  assert(result.binding.generation && result.binding.request_id);
  const initial = await snapshot('defaults');
  assert.equal(initial.first.value, 'default-value'); assert.equal(initial.second.value, 'default-value');
  assert.equal(initial.first.language, 'default-language'); assert.equal(initial.language.language, 'default-typescript');
  assert.equal(initial.first.inspect.defaultValue, 'default-value');
  assert.equal(initial.language.languageInspect.defaultLanguageValue, 'default-typescript');
  assert.equal(initial.first.folders.length, 2);

  await phase('public-global-workspace-and-folder-writers');
  await update({ key: 'value', value: 'global-value', target: 'Global' });
  const global = await snapshot('global');
  for (const value of [global.first, global.second]) {
    assert.equal(value.value, 'global-value'); assert.equal(value.inspect.globalValue, 'global-value');
    assert.equal(value.inspect.globalRemoteValue, 'global-value'); assert.equal(value.inspect.globalLocalValue, null);
  }
  await update({ key: 'value', value: 'workspace-value', target: 'Workspace' });
  const workspace = await snapshot('workspace');
  assert.equal(workspace.first.value, 'workspace-value'); assert.equal(workspace.second.value, 'workspace-value');
  assert.equal(workspace.first.inspect.workspaceValue, 'workspace-value');
  await update({ folder: 0, key: 'value', value: 'folder-value', target: 'WorkspaceFolder' });
  const folder = await snapshot('folder');
  assert.equal(folder.first.value, 'folder-value'); assert.equal(folder.second.value, 'workspace-value');
  assert.equal(folder.first.inspect.workspaceFolderValue, 'folder-value'); assert.equal(folder.second.inspect.workspaceFolderValue, null);
  await update({ folder: 0, key: 'value', value: 'inferred-folder' });
  await update({ key: 'inferred', value: 'inferred-view' });
  await update({ folder: 0, language: 'typescript', key: 'language', value: 'folder-typescript',
    target: 'WorkspaceFolder', overrideInLanguage: true });
  const inferred = await snapshot('inference-and-language');
  assert.equal(inferred.first.value, 'inferred-folder'); assert.equal(inferred.second.value, 'workspace-value');
  assert.equal(inferred.first.inferredInspect.workspaceValue, 'inferred-view');
  assert.equal(inferred.first.language, 'default-language'); assert.equal(inferred.language.language, 'folder-typescript');
  assert.equal(inferred.language.languageInspect.workspaceFolderLanguageValue, 'folder-typescript');
  assert.equal((await read({ folder: 1, language: 'typescript' })).language, 'default-typescript');

  await phase('external-edit-updates-same-worker-model');
  const native = new URL(result.binding.context.workspace.configuration.native_uri);
  const content = parse(await readFile(native, 'utf8'));
  assert.equal(content.settings['webScopeProbe.value'], 'workspace-value');
  assert.equal(content.settings['webScopeProbe.inferred'], 'inferred-view');
  content.settings['webScopeProbe.value'] = 'external-view';
  // 仅模拟用户外部编辑：关键文件及先前设置都已由产品原 writer 生成。
  await writeFile(native, JSON.stringify(content, null, 2) + '\n');
  await expect.poll(async () => (await read({ folder: 1 })).value, { timeout: 15_000 }).toBe('external-view');
  const external = await snapshot('external');
  assert.equal(external.first.value, 'inferred-folder');
  assert(external.first.events.some(event => event.first === 'inferred-folder' && event.second === 'external-view'));
  assert(result.snapshots.every(entry => [entry.first, entry.second, entry.language]
    .every(value => value.activation === initial.first.activation && value.worker)));

  await phase('reject-foreign-resources-without-profile-fallback');
  await Promise.all(pending);
  const writes = result.writes.length;
  const foreignGateway = new URL(initial.first.folders[0]); foreignGateway.hostname = 'gateway_0123456789abcdef0123456789abcdef';
  const foreignWorkspace = new URL(initial.first.folders[0]);
  foreignWorkspace.pathname = foreignWorkspace.pathname.replace('/' + fixture.workspace_ids[0] + '/', '/non-member-workspace/');
  for (const uri of [foreignGateway.href, foreignWorkspace.href, 'file:///tmp/outside-target-view.ts']) {
    await assert.rejects(update({ uri, key: 'value', value: 'must-not-write', target: 'Global' }),
      error => { result.rejectedResources.push({ uri, error: String(error) }); return /配置资源不属于发起宿主/.test(String(error)); });
  }
  await Promise.all(pending); assert.equal(result.writes.length, writes);
  const afterRejected = await snapshot('after-rejection');
  assert.equal(afterRejected.first.value, 'inferred-folder'); assert.equal(afterRejected.second.value, 'external-view');
  assert.equal(afterRejected.first.inspect.globalValue, 'global-value');
  assert(result.writes.length > 0);
  for (const receipt of result.writes) {
    assert.equal(receipt.status, 200); assert.equal(receipt.request.gateway_id, fixture.gateway_id);
    assert.equal(receipt.request.backend_id, fixture.backend_id);
    assert.equal(receipt.request.view_id, result.binding.view_id);
    assert.equal(receipt.request.generation, result.binding.generation);
  }
  assert.deepEqual(result.nodeChannels, []); assert.deepEqual(result.pageErrors, []);
  await phase('complete');
} catch (error) {
  failure = error; result.error = String(error.stack ?? error);
  if (page) {
    const diagnostics = await Promise.allSettled([
      page.screenshot({ path: output.replace(/\.json$/, '.failed.png'), fullPage: true }),
      page.locator('body').innerText().then(text => { result.visible = text; }),
    ]);
    const errors = diagnostics.filter(value => value.status === 'rejected').map(value => value.reason);
    if (errors.length) failure = new AggregateError([failure, ...errors], '配置作用域验收及诊断失败');
  }
} finally {
  const collected = await Promise.allSettled(pending);
  const errors = collected.filter(value => value.status === 'rejected').map(value => value.reason);
  if (errors.length) failure = new AggregateError([...(failure ? [failure] : []), ...errors], '配置响应校验失败');
  try { await save(); } catch (error) { failure = failure ? new AggregateError([failure, error], '配置验收及产物保存失败') : error; }
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '配置验收及清理失败') : error; }
}
if (failure) throw failure;
