import assert from 'node:assert/strict';
import { parse as parseJsonc } from 'jsonc-parser';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const input = JSON.parse(process.env.BOXTEAM_COMMAND_UI_INPUT);
const receipt = { phase: 'initialization', pageErrors: [], requests: [], exitedPids: [], uninstalledVersions: [] };
const warmSampleCount = 20;
const extraExecutions = warmSampleCount - 1;
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  const openWindow = async () => {
    page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
    page.setDefaultTimeout(15_000);
    page.on('pageerror', error => receipt.pageErrors.push(error.message));
    page.on('request', request => {
      if (request.url().includes('/service-plugins/')) receipt.requests.push({ url: request.url(), method: request.method(), body: request.postData() });
    });
    await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  };
  await openWindow();
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (path, options = {}) => page.evaluate(async ({ path, options }) => {
    const auth = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', 'X-Local-Token': auth.data.token },
      body: options.body && JSON.stringify(options.body) });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(`${path}: ${response.status} ${JSON.stringify(body)}`);
    return body.data;
  }, { path, options });
  receipt.initialization = [];
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) receipt.initialization.push(await api(path));
  const gatewayId = receipt.initialization[0].gateway_id;
  const install = async version => {
    const installing = command('boxteam.plugins.installPackage');
    const installPicker = page.locator('.quick-input-widget').filter({ visible: true });
    await expect(installPicker).toBeVisible();
    await installPicker.getByRole('option').filter({ hasText: 'ZIP 文件' }).click();
    await expect(installPicker.locator('input')).toBeVisible();
    await installPicker.locator('input').fill(input.packagePaths[version]);
    await page.keyboard.press('Enter');
    await installing;
  };
  receipt.phase = 'install-package';
  await install('1.0.0');
  const installed = await api('/api/gateway/service-plugins/inventory', { method: 'POST', body: { gateway_id: gatewayId } });
  assert(installed.packages.some(pkg => pkg.manifest.package_id === 'proof.command-ui'));
  let revision = 0;
  const bind = async (enabled, version = '1.0.0') => {
    const path = '/api/gateway/config/sources/gateway_mutable_override', document = await api(path);
    const config = parseJsonc(document.jsonc);
    // 复用既有 Trace 日志关联软件内总耗时，不为量测新增生产观测机制。
    config.logging = { runtime: { level: 'debug', pretty: true } };
    config.plugins = { ...config.plugins, runtime: { entries: enabled === null ? [] : [{ package_id: 'proof.command-ui', plugin_id: 'commands', package_version: version, enabled }] },
      configuration: { values: { 'proof.command-ui': { commands: { output: input.output } } } } };
    const saved = await api(path, { method: 'PUT', body: { gateway_id: gatewayId, source_key: 'gateway_mutable_override', jsonc: JSON.stringify(config),
      base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
      expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: `command-ui-${++revision}` } });
    assert.equal(saved.edit.state, 'active');
    return saved;
  };
  const runtime = async () => (await api('/api/gateway/service-plugins/status', {
    method: 'POST', body: { gateway_id: gatewayId },
  })).plugins.find(item => item.binding.package_id === 'proof.command-ui');
  const publications = async () => (await api('/api/gateway/service-plugins/commands/publications', {
    method: 'POST', body: { gateway_id: gatewayId },
  })).publications;
  const enabledAt = performance.now();
  await bind(true);
  receipt.phase = 'palette';
  let palette = page.locator('.quick-input-widget').filter({ visible: true });
  const openPalette = async (title = '命令验收：执行') => {
    // 每次观察都重新打开；启动未就绪时捕获的空 owner 不应被同一次浮层转接改写。
    if (await palette.isVisible()) await page.keyboard.press('Escape');
    await command('workbench.action.showCommands');
    await palette.locator('input').fill('>' + title);
  };
  await expect.poll(async () => {
    await openPalette();
    return palette.getByRole('option').filter({ hasText: '命令验收：执行' }).count();
  }).toBe(1);
  receipt.enabledToPaletteMs = performance.now() - enabledAt;
  // 命令面板只交冻结来源，不合成业务参数；空对象由声明允许。
  await palette.getByRole('option').filter({ hasText: '命令验收：执行' }).click();
  const countExecutions = async () => { try { return (await readFile(input.output, 'utf8')).trim().split('\n').length; } catch (error) { if (error.code === 'ENOENT') return 0; throw error; } };
  await expect.poll(countExecutions).toBe(1);
  receipt.enabledToFirstResultMs = performance.now() - enabledAt;
  const id = 'boxteam.plugin:proof.command-ui/commands/execute';
  receipt.warmCommands = [];
  const beforeWarm = receipt.requests.length;
  for (let index = 0; index < warmSampleCount; index++) {
    const response = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/commands/call'));
    const started = performance.now();
    const result = await command(id, { value: `programmatic-${index}` });
    const milliseconds = performance.now() - started;
    assert.equal(result.value, `programmatic-${index}`); assert.equal(result.version, '1.0.0');
    const completed = await response;
    assert.equal(completed.status(), 200);
    const requestId = completed.headers()['x-request-id'], call = completed.request().postDataJSON();
    assert(requestId);
    receipt.warmCommands.push({ requestId, callId: call.call_id, milliseconds });
    receipt.result = result;
  }
  assert.equal(receipt.requests.length - beforeWarm, warmSampleCount);
  assert(receipt.requests.slice(beforeWarm).every(request => new URL(request.url).pathname.endsWith('/commands/call')),
    '连续暖调用只能发送原业务请求，不逐调用发现、状态探活或库存审计');
  const result = receipt.result;
  const original = await runtime();
  assert.equal(original.instance.process_id, result.pid);
  await expect.poll(countExecutions).toBe(2 + extraExecutions);
  receipt.warmReads = await page.evaluate(async ({ gatewayId, count }) => {
    const auth = await (await fetch('/api/gateway/auth/local-credential')).json();
    const samples = [];
    for (const [operation, path, options] of [
      ['status', '/api/gateway/service-plugins/status', { method: 'POST', body: JSON.stringify({ gateway_id: gatewayId }) }],
      ['settings', '/api/gateway/config/sources/gateway_mutable_override', {}],
    ]) {
      // 鉴权准备和首次读取不计入暖样本；每次都消费完整真实响应。
      for (let index = -1; index < count; index++) {
        const started = performance.now();
        const response = await fetch(path, { ...options,
          headers: { 'Content-Type': 'application/json', 'X-Local-Token': auth.data.token } });
        const text = await response.text(), body = JSON.parse(text);
        if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) {
          throw new Error(`暖读取失败: ${path}: ${response.status} ${text}`);
        }
        const milliseconds = performance.now() - started;
        if (index >= 0) samples.push({ operation, requestId: body.request_id, milliseconds,
          bytes: new TextEncoder().encode(text).length });
      }
    }
    return samples;
  }, { gatewayId, count: warmSampleCount });
  receipt.workload = { installedPackages: installed.packages.length, activeBindings: 1, commandSamples: warmSampleCount,
    readSamplesPerOperation: warmSampleCount, logging: 'debug', enabledToFirstResultMs: receipt.enabledToFirstResultMs };
  receipt.phase = 'keybinding';
  const roots = await api('/api/v1/workspace/files/roots');
  await command('workbench.action.quickOpen', roots.workspace_uri + '/command-ui.txt');
  const filePick = page.locator('.quick-input-widget').filter({ visible: true });
  await filePick.getByText('打开链接', { exact: true }).click();
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).first();
  await expect(editor).toBeVisible(); await editor.click();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Alt+Shift+9' : 'Control+Alt+Shift+9');
  await expect.poll(countExecutions).toBe(3 + extraExecutions);
  receipt.phase = 'menu';
  await editor.click({ button: 'right' });
  const menu = page.getByRole('menuitem').filter({ hasText: '命令验收：执行' });
  await expect(menu).toBeVisible();
  await menu.press('Enter');
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Alt+Shift+9' : 'Control+Alt+Shift+9');
  await expect.poll(countExecutions).toBe(4 + extraExecutions);
  assert.equal(receipt.requests.filter(request => request.url.endsWith('/commands/events')).length, 2);

  receipt.phase = 'update-installed-only';
  await install('2.0.0');
  assert.equal((await runtime()).instance.instance_id, original.instance.instance_id);
  assert.equal((await publications())[0].binding.package_version, '1.0.0');
  await openPalette();
  await expect(palette.getByRole('option').filter({ hasText: '命令验收：执行' })).toHaveCount(1);
  await page.keyboard.press('Escape');
  receipt.installedVersionKeptBinding = true;

  receipt.phase = 'window-reopen';
  const originalWindow = page;
  await openWindow();
  palette = page.locator('.quick-input-widget').filter({ visible: true });
  await api('/api/v1/workspace');
  assert.equal((await runtime()).instance.instance_id, original.instance.instance_id);
  await expect.poll(async () => {
    await openPalette();
    return palette.getByRole('option').filter({ hasText: '命令验收：执行' }).count();
  }).toBe(1);
  await originalWindow.close();
  assert.equal((await runtime()).instance.instance_id, original.instance.instance_id);
  await palette.getByRole('option').filter({ hasText: '命令验收：执行' }).click();
  await expect.poll(countExecutions).toBe(5 + extraExecutions);
  receipt.windowReopenedSameInstance = true;

  receipt.phase = 'switch-bound-version';
  await bind(true, '2.0.0');
  const updated = await runtime();
  assert.equal(updated.state, 'ready');
  assert.equal(updated.binding.package_version, '2.0.0');
  assert.notEqual(updated.instance.instance_id, original.instance.instance_id);
  receipt.exitedPids.push(original.instance.process_id);
  await expect.poll(async () => {
    await openPalette('命令验收：新版执行');
    return palette.getByRole('option').filter({ hasText: '命令验收：新版执行' }).count();
  }).toBe(1);
  await palette.getByRole('option').filter({ hasText: '命令验收：新版执行' }).click();
  await expect.poll(countExecutions).toBe(6 + extraExecutions);
  await openPalette();
  await expect(palette.getByRole('option').filter({ hasText: '命令验收：执行' })).toHaveCount(0);
  await page.keyboard.press('Escape');
  await command('workbench.action.quickOpen', roots.workspace_uri + '/command-ui.txt');
  await palette.getByText('打开链接', { exact: true }).click();
  const updatedEditor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).first();
  await expect(updatedEditor).toBeVisible(); await updatedEditor.click();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Alt+Shift+9' : 'Control+Alt+Shift+9');
  const current = await command(id, { value: 'after-old-key' });
  assert.equal(current.version, '2.0.0');
  assert.equal(current.pid, updated.instance.process_id);
  // 后续命令响应是旧键盘事件的顺序屏障；旧键位不得留下额外一次调用。
  assert.equal(await countExecutions(), 7 + extraExecutions);
  await updatedEditor.click();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Alt+Shift+8' : 'Control+Alt+Shift+8');
  await expect.poll(countExecutions).toBe(8 + extraExecutions);

  receipt.phase = 'disabled';
  await openPalette('命令验收：新版执行');
  await expect(palette.getByRole('option').filter({ hasText: '命令验收：新版执行' })).toHaveCount(1);
  await bind(false, '2.0.0');
  await expect.poll(async () => (await publications()).length).toBe(0);
  // 已打开的 pick 仍带原资格；接受也不能启动、改投或增加一次执行。
  await page.keyboard.press('Enter');
  await expect(palette).not.toBeVisible();
  assert.equal(await countExecutions(), 8 + extraExecutions);
  await expect.poll(async () => {
    await openPalette('命令验收：新版执行');
    return palette.getByRole('option').filter({ hasText: '命令验收：新版执行' }).count();
  }).toBe(0);
  await page.keyboard.press('Escape');
  await assert.rejects(command(id, {}));
  assert.equal((await runtime()).state, 'stopped');
  receipt.exitedPids.push(updated.instance.process_id);
  receipt.phase = 'unbind-uninstall';
  await assert.rejects(api('/api/gateway/service-plugins/uninstall', { method: 'POST', body: {
    gateway_id: gatewayId, package_id: 'proof.command-ui', package_version: '2.0.0',
  } }), /409/);
  await bind(null);
  for (const version of ['1.0.0', '2.0.0']) {
    const removed = await api('/api/gateway/service-plugins/uninstall', { method: 'POST', body: {
      gateway_id: gatewayId, package_id: 'proof.command-ui', package_version: version,
    } });
    assert.equal(removed.uninstalled, true);
    assert.equal(removed.package_version, version);
    receipt.uninstalledVersions.push(version);
  }
  const source = await api('/api/gateway/config/sources/gateway_mutable_override');
  assert.equal(parseJsonc(source.jsonc).plugins.configuration.values['proof.command-ui'].commands.output, input.output);
  assert.equal((await publications()).length, 0);
  // 每个窗口各一个 Gateway 和 workspace 持续流；普通命令没有额外发现。
  assert.equal(receipt.requests.filter(request => request.url.endsWith('/commands/events')).length, 4);
  assert.equal(receipt.requests.filter(request => request.url.endsWith('/commands/registrations')).length, 0);
  assert.equal(receipt.requests.filter(request => request.url.endsWith('/commands/call')).length, 8 + extraExecutions);
  receipt.phase = 'complete';
} catch (error) {
  receipt.error = String(error.stack ?? error); process.exitCode = 1;
} finally {
  const failures = [];
  if (page && receipt.phase !== 'complete') try { await page.screenshot({ path: join(input.artifacts, 'command-ui-failure.png') }); } catch (error) { failures.push(error); }
  try { await writeFile(join(input.artifacts, 'command-ui.json'), JSON.stringify(receipt, null, 2)); } catch (error) { failures.push(error); }
  try { await browser?.close(); } catch (error) { failures.push(error); }
  if (receipt.error) console.error(receipt.error);
  if (failures.length) throw new AggregateError(failures, '命令验收诊断或关闭失败');
}
