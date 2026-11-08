import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_TARGET_SETTINGS);
const output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
assert(fixture?.base_url && fixture.base_url_b && fixture.targets && output, '必须显式提供双目标和产物目录');
const run = await TestRunContext.fromTestFile('tests/integration/clients/web/test_empty_target_web.py').prepare();
const observations = () => ({ pageErrors: [], nodeChannels: [], nodeResolves: [], writes: [], preparations: [] });
const result = { phase: 'initialize', a: observations(), b: observations(), receipts: [],
  boundary: '正式 Workbench、真实双 Gateway/Backend、原安装器与 Web Worker；A 设置 UI 经 Relay 写 B，B 公开配置 API 反向通知 A。SSH/隧道是具名 loopback 装具，不代替真实 SSH、Windows 或 Electron。' };
const pending = [], pages = [];
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const phase = async value => { result.phase = value; await save(); };
const command = (page, id, ...args) => page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
const query = (page, id, ...args) => page.evaluate(async ({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
let failure;

async function api(page, path, method = 'GET', payload) {
  const receipt = await page.evaluate(async ({ path, method, payload }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token },
      ...(payload ? { body: JSON.stringify(payload) } : {}) });
    return { status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
  }, { path, method, payload });
  result.receipts.push(receipt);
  assert.equal(receipt.status, 200); assert(receipt.body.request_id); assert.equal(receipt.body.request_id, receipt.header);
  return receipt.body.data;
}

async function open(browser, base, state, target) {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('目标设置 BrowserContext ' + target.gateway_id, () => context.close());
  const page = await context.newPage(); pages.push(page);
  page.on('pageerror', error => state.pageErrors.push(String(error.stack ?? error)));
  page.on('websocket', socket => { if (new URL(socket.url()).pathname === '/api/gateway/extension-hosts/channel') state.nodeChannels.push(socket.url()); });
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/extension-configurations/write') state.writes.push(request.postDataJSON());
    if (path === '/api/gateway/extension-hosts/resolve') state.nodeResolves.push(request.postDataJSON());
  });
  page.on('response', response => {
    if (new URL(response.url()).pathname !== '/api/gateway/extension-configurations/prepare' || response.status() !== 200) return;
    pending.push(response.json().then(body => {
      assert(body.request_id); assert.equal(body.request_id, response.headers()['x-request-id']); state.preparations.push(body.data);
    }));
  });
  await page.goto(new URL('/api/gateway/health', base).href);
  await api(page, '/api/gateway/users/guest', 'POST', { tracking: { source: 'target-settings-federation' } });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const health = await api(page, '/api/gateway/health'), workspaces = await api(page, '/api/gateway/workspaces');
  assert.equal(health.gateway_id, target.gateway_id); assert.equal(workspaces.active_workspace.gateway_id, target.gateway_id);
  await api(page, '/api/v1/workspace');
  return page;
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--renderer-process-limit=2'] });
  run.addCleanup('目标设置 Chromium', () => browser.close());
  const a = await open(browser, fixture.base_url, result.a, fixture.targets.a);
  const b = await open(browser, fixture.base_url_b, result.b, fixture.targets.b);
  const read = page => query(page, 'boxteam.settingsProbe.read');
  await expect.poll(async () => (await read(a)).value, { timeout: 20_000 }).toBe('target-default');
  await expect.poll(async () => (await read(b)).value, { timeout: 20_000 }).toBe('target-default');
  await phase('choose-remote-settings');
  await command(a, 'workbench.action.openSettings2');
  const editor = a.locator('.settings-editor:visible');
  await expect(editor).toBeVisible();
  await editor.getByRole('button', { name: '目标：本地 · 当前工作台', exact: true }).click();
  const picker = a.locator('.quick-input-widget:visible');
  await expect(picker).toBeVisible();
  await picker.locator('.monaco-list-row').filter({ hasText: 'Settings Target B' }).click();
  const targetName = '目标：Settings Target B · ' + fixture.targets.b.backend_id;
  await expect(editor.getByRole('button', { name: targetName, exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(editor.locator('.settings-context-label')).toContainText('范围：目标用户');
  await Promise.all(pending);
  result.binding = result.a.preparations.at(-1);
  assert.equal(result.binding.gateway_id, fixture.targets.b.gateway_id);
  assert.equal(result.binding.backend_id, fixture.targets.b.backend_id);
  await command(a, 'settings.action.search');
  await a.keyboard.insertText('@ext:boxteam-probe.settings-browser @id:targetSettingsProbe.value');
  const input = editor.locator('[data-key="targetSettingsProbe.value"] .setting-item-control input');
  await expect(input).toBeVisible(); await expect(input).toHaveValue('target-default');
  await phase('ui-writes-B-worker-reads');
  await input.fill('from-A-UI');
  await command(a, 'workbench.action.files.save');
  await expect.poll(async () => (await read(b)).value, { timeout: 20_000 }).toBe('from-A-UI');
  const remote = await read(b);
  assert(remote.worker); assert.equal(remote.inspect.globalValue, 'from-A-UI');
  assert.equal(remote.inspect.globalRemoteValue, 'from-A-UI');
  assert.equal(remote.inspect.globalLocalValue, undefined);
  assert(remote.events.includes('from-A-UI'));
  result.afterUiWrite = remote;
  result.localValue = (await read(a)).value;
  assert.equal(result.localValue, 'target-default');
  await phase('worker-writes-original-ui-observes');
  // 原 SettingsEditor 对正在编辑的控件延迟重绘；移到搜索框后核真实外部通知。
  await command(a, 'settings.action.search');
  assert(await input.evaluate(element => element !== element.ownerDocument.activeElement));
  // B 的原公开 API 写同一 profile；A 当前编辑器继续绑定 B，不借页面焦点重选。
  await b.bringToFront();
  const updated = await query(b, 'boxteam.settingsProbe.update', 'from-B-worker');
  assert.equal(updated.value, 'from-B-worker');
  await expect(input).toHaveValue('from-B-worker', { timeout: 20_000 });
  assert.equal((await read(a)).value, 'target-default');
  await command(a, 'settings.action.search'); await a.keyboard.press('Control+A');
  await a.keyboard.insertText('@modified @ext:boxteam-probe.settings-browser');
  await expect(input).toBeVisible(); await expect(input).toHaveValue('from-B-worker');
  await expect(editor.getByRole('button', { name: targetName, exact: true })).toBeVisible();
  result.uiValue = await input.inputValue(); result.remoteValue = (await read(b)).value;
  result.afterWorkerWrite = await read(b);
  for (const state of [result.a, result.b]) {
    assert.deepEqual(state.pageErrors, []); assert.deepEqual(state.nodeChannels, []); assert.deepEqual(state.nodeResolves, []);
  }
  assert(result.a.writes.length > 0);
  for (const request of result.a.writes) {
    assert.equal(request.gateway_id, fixture.targets.b.gateway_id); assert.equal(request.backend_id, fixture.targets.b.backend_id);
    assert.equal(request.resource_uri, result.binding.context.remote_settings.resource_uri); assert(!('generation' in request));
  }
  await a.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
  await phase('complete');
} catch (error) {
  failure = error; result.error = String(error.stack ?? error);
  const captures = await Promise.allSettled(pages.map((page, index) => page.screenshot({ path: output.replace(/\.json$/, '-failure-' + index + '.png'), fullPage: true })));
  result.screenshotErrors = captures.filter(value => value.status === 'rejected').map(value => String(value.reason));
} finally {
  const observed = await Promise.allSettled(pending);
  const errors = observed.filter(value => value.status === 'rejected').map(value => value.reason);
  if (errors.length) failure = new AggregateError([...(failure ? [failure] : []), ...errors], '目标设置回执观察失败');
  await save();
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '目标设置验证与清理失败') : error; }
}
if (failure) throw failure;
