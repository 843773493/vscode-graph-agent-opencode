import assert from 'node:assert/strict';
import { parse as parseJsonc } from 'jsonc-parser';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const input = JSON.parse(process.env.BOXTEAM_COMMAND_UI_INPUT);
const receipt = { phase: 'initialization', pageErrors: [], requests: [] };
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => receipt.pageErrors.push(error.message));
  page.on('request', request => {
    if (request.url().includes('/service-plugins/commands/')) receipt.requests.push({ url: request.url(), method: request.method(), body: request.postData() });
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
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
  receipt.phase = 'install-package';
  const installing = command('boxteam.plugins.installPackage');
  const installPicker = page.locator('.quick-input-widget').filter({ visible: true });
  await expect(installPicker).toBeVisible();
  await installPicker.getByRole('option').filter({ hasText: 'ZIP 文件' }).click();
  await expect(installPicker.locator('input')).toBeVisible();
  await installPicker.locator('input').fill(input.packagePath);
  await page.keyboard.press('Enter');
  await installing;
  const installed = await api('/api/gateway/service-plugins/inventory', { method: 'POST', body: { gateway_id: gatewayId } });
  assert(installed.packages.some(pkg => pkg.manifest.package_id === 'proof.command-ui'));
  let revision = 0;
  const bind = async enabled => {
    const path = '/api/gateway/config/sources/gateway_mutable_override', document = await api(path);
    const config = parseJsonc(document.jsonc);
    config.plugins = { ...config.plugins, runtime: { entries: [{ package_id: 'proof.command-ui', plugin_id: 'commands', package_version: '1.0.0', enabled }] },
      configuration: { values: { 'proof.command-ui': { commands: { output: input.output } } } } };
    return api(path, { method: 'PUT', body: { gateway_id: gatewayId, source_key: 'gateway_mutable_override', jsonc: JSON.stringify(config),
      base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
      expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: `command-ui-${++revision}` } });
  };
  const enabledAt = performance.now();
  await bind(true);
  receipt.phase = 'palette';
  const palette = page.locator('.quick-input-widget').filter({ visible: true });
  const openPalette = async () => {
    await command('workbench.action.showCommands');
    await palette.locator('input').fill('>命令验收：执行');
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
  const id = 'boxteam.plugin:proof.command-ui/commands/execute';
  const warmAt = performance.now();
  const result = await command(id, { value: 'programmatic' });
  receipt.warmCommandMs = performance.now() - warmAt;
  assert.equal(result.value, 'programmatic'); receipt.result = result;
  await expect.poll(countExecutions).toBe(2);
  receipt.phase = 'keybinding';
  const roots = await api('/api/v1/workspace/files/roots');
  await command('workbench.action.quickOpen', roots.workspace_uri + '/command-ui.txt');
  const filePick = page.locator('.quick-input-widget').filter({ visible: true });
  await filePick.getByText('打开链接', { exact: true }).click();
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).first();
  await expect(editor).toBeVisible(); await editor.click();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Alt+Shift+9' : 'Control+Alt+Shift+9');
  await expect.poll(countExecutions).toBe(3);
  receipt.phase = 'menu';
  await editor.click({ button: 'right' });
  const menu = page.getByRole('menuitem').filter({ hasText: '命令验收：执行' });
  await expect(menu).toBeVisible(); await menu.click();
  await expect.poll(countExecutions).toBe(4);
  receipt.phase = 'disabled';
  await openPalette();
  await expect(palette.getByRole('option').filter({ hasText: '命令验收：执行' })).toHaveCount(1);
  await bind(false);
  await expect.poll(async () => (await api('/api/gateway/service-plugins/commands/publications', {
    method: 'POST', body: { gateway_id: gatewayId } })).publications.length).toBe(0);
  // 已打开的 pick 仍带原资格；接受也不能启动、改投或写入第五次。
  await page.keyboard.press('Enter');
  await expect(palette).not.toBeVisible();
  assert.equal(await countExecutions(), 4);
  await expect.poll(async () => {
    await openPalette();
    return palette.getByRole('option').filter({ hasText: '命令验收：执行' }).count();
  }).toBe(0);
  await page.keyboard.press('Escape');
  await assert.rejects(command(id, {}));
  // 无菜单/键位重装或每次调用发现；单 Gateway 和单 workspace 各一个持续流。
  assert.equal(receipt.requests.filter(request => request.url.endsWith('/commands/events')).length, 2);
  assert.equal(receipt.requests.filter(request => request.url.endsWith('/commands/registrations')).length, 0);
  assert.equal(receipt.requests.filter(request => request.url.endsWith('/commands/call')).length, 4);
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
