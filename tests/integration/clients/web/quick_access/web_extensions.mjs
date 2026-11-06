import assert from 'node:assert/strict';
import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

if (process.argv[2] === '--prepare-assets') {
  const [, , , source, target] = process.argv;
  await cp(source, target, { recursive: true });
  for (const version of ['1.0.0', '2.0.0', 'node-only']) {
    const directory = join(target, 'extensions', `cache-probe-${version}`);
    await mkdir(directory, { recursive: true });
    const manifest = { publisher: 'boxteam-test', name: 'cache-probe', displayName: 'Cache Lifecycle Probe',
      // TODO（extension-management 4.1）：原安装链可保存engine不兼容记录，但不会激活，仍需前置拒绝验收。
      version: version === 'node-only' ? '3.0.0' : version, engines: { vscode: '^1.145.0' },
      ...(version === 'node-only' ? { main: './extension.js', extensionKind: ['workspace'] } : { browser: './extension.js' }),
      contributes: { commands: [{ command: 'boxteamCacheProbe.inspect', title: 'Inspect Cache Probe' }] } };
    await writeFile(join(directory, 'package.json'), JSON.stringify(manifest));
    await writeFile(join(directory, 'package.nls.json'), '{}');
    // 测试源码用ESM；仅产物遵循固定上游Web Worker仍要求的CommonJS扩展格式。
    await build({ stdin: { contents: `import * as vscode from 'vscode';
export function activate(context) {
context.subscriptions.push(vscode.commands.registerCommand('boxteamCacheProbe.inspect', () => ({
id: context.extension.id, version: context.extension.packageJSON.version,
host: typeof WorkerGlobalScope !== 'undefined' && globalThis instanceof WorkerGlobalScope ? 'web-worker' : 'unexpected'
}))); }` }, outfile: join(directory, 'extension.js'), bundle: true, platform: 'browser', format: 'cjs', external: ['vscode'], logLevel: 'silent' });
  }
  const theme = join(target, 'extensions/cache-probe-theme');
  await mkdir(theme, { recursive: true });
  await writeFile(join(theme, 'package.json'), JSON.stringify({ publisher: 'boxteam-test', name: 'cache-theme',
    version: '1.0.0', displayName: 'Cache Probe Theme', engines: { vscode: '^1.145.0' },
    contributes: { themes: [{ label: 'Cache Probe Theme', uiTheme: 'vs-dark', path: './theme.json' }] } }));
  await writeFile(join(theme, 'package.nls.json'), '{}');
  await writeFile(join(theme, 'theme.json'), JSON.stringify({ colors: { 'editor.background': '#123456' }, tokenColors: [] }));
  assert.deepEqual(await readFile(join(source, 'out/main.js')), await readFile(join(target, 'out/main.js')));
  process.exit(0);
}

const required = key => { assert(process.env[key], `缺少${key}`); return process.env[key]; };
const output = required('BOXTEAM_BROWSER_RESULT_PATH'), base = required('BOXTEAM_BROWSER_BASE_URL');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_web_extensions.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], consoleErrors: [], requests: [], executions: [],
  boundary: '真实Gateway／Backend／正式Workbench及原Web扩展宿主；只添加显式ESM测试扩展的生态编译产物，无HTTP响应替身或预写安装记录' };
let context, page, failure;
const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
  await globalThis.workbench.commands.executeCommand(id, ...args);
}, { id, args });
async function open() {
  context = await chromium.launchPersistentContext(join(required('BOXTEAM_BROWSER_RUNTIME_PATH'), 'extension-profile'), {
    executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    viewport: { width: 1600, height: 1050 }, permissions: ['clipboard-read', 'clipboard-write'], args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  page = context.pages()[0] ?? await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', entry => { if (entry.type() === 'error') result.consoleErrors.push(entry.text()); });
  context.on('response', response => {
    if (response.url().includes('/cache-probe-') || response.url().includes('/extensionHostWorkerMain.js')) {
      result.requests.push({ phase: result.phase, url: response.url(), status: response.status() });
    }
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  // React导航先于原Restored贡献就绪；只观察原命令注册，不用固定sleep冒充可操作。
  await expect.poll(() => page.evaluate(async () => {
    const actions = await globalThis.workbench.commands.executeCommand('_getAllCommands');
    return actions.some(action => action.command === 'workbench.extensions.action.installExtensionFromLocation');
  }), { timeout: 30_000 }).toBe(true);
}
async function install(version) {
  const outcome = page.evaluate(async () => {
    try { await globalThis.workbench.commands.executeCommand('workbench.extensions.action.installExtensionFromLocation'); return { ok: true }; }
    catch (error) { return { ok: false, error: String(error) }; }
  });
  const input = page.locator('.quick-input-widget input');
  await Promise.race([
    expect(input).toBeVisible({ timeout: 30_000 }),
    outcome.then(observed => { throw new Error(`原安装命令在出现输入前结束：${JSON.stringify(observed)}`); }),
  ]);
  await input.fill(`${base}/extensions/cache-probe-${version}`);
  await input.press('Enter');
  return outcome;
}
async function inspect(version) {
  const observed = await page.evaluate(() => globalThis.workbench.commands.executeCommand('boxteamCacheProbe.inspect'));
  expect(observed).toEqual({ id: 'boxteam-test.cache-probe', version, host: 'web-worker' });
  result.executions.push(observed);
}
const themeColor = () => page.evaluate(() => getComputedStyle(document.querySelector('.monaco-workbench'))
  .getPropertyValue('--vscode-editor-background').trim());
async function profileText() {
  await command('workbench.extensions.action.openInstalledWebExtensionsResource');
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await expect(editor).toBeVisible();
  await editor.getByRole('textbox').focus();
  await page.keyboard.press('ControlOrMeta+A');
  await command('editor.action.clipboardCopyAction');
  return { editor, text: await page.evaluate(() => navigator.clipboard.readText()) };
}
async function replaceProfile(text) {
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  await editor.getByRole('textbox').focus();
  await page.keyboard.press('ControlOrMeta+A');
  // 使用真实粘贴，避免Monaco把逐字输入当作自动补括号操作。
  await page.evaluate(text => navigator.clipboard.writeText(text), text);
  // 等待原粘贴命令完成读取剪贴板，避免异步粘贴尚未落入模型就保存旧正文。
  await command('editor.action.clipboardPasteAction');
  await expect(editor.locator('.view-lines')).toContainText(text.includes('unknownFormat') ? 'unknownFormat' : 'boxteam-test.cache-probe');
  await command('workbench.action.files.save');
}
try {
  run.addCleanup('persistent-extension-context', async () => { await context?.close(); });
  await open();
  result.initialization = await page.evaluate(async () => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const observed = [];
    let owner;
    for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
      const response = await fetch(path, { headers: { 'X-Local-Token': token, ...(owner && path.startsWith('/api/v1/') ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id,
      } : {}) } });
      const body = await response.json();
      if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, body }));
      if (path.endsWith('/workspaces')) owner = body.data.active_workspace;
      if (path === '/api/v1/workspace' && body.data.workspace_id !== owner.workspace_id) throw new Error('初始化目标不一致');
      observed.push({ path, requestId: body.request_id, owner });
    }
    return observed;
  });
  result.phase = 'install-v1';
  expect(await install('1.0.0')).toEqual({ ok: true });
  await inspect('1.0.0');
  result.phase = 'declarative-theme';
  expect(await install('theme')).toEqual({ ok: true });
  const selected = command('workbench.action.selectTheme');
  const themeInput = page.locator('.quick-input-widget input');
  await expect(themeInput).toBeVisible();
  await themeInput.fill('Cache Probe Theme');
  await expect(page.locator('.quick-input-widget').getByText('Cache Probe Theme', { exact: true })).toBeVisible();
  await themeInput.press('Enter');
  await selected;
  await expect.poll(themeColor).toBe('#123456');
  result.declarativeTheme = { installed: true, color: await themeColor() };
  result.phase = 'normal-close-and-reopen';
  await context.close(); context = undefined;
  await open();
  await inspect('1.0.0');
  await expect.poll(themeColor).toBe('#123456');
  result.declarativeTheme.restored = true;
  result.phase = 'explicit-upgrade';
  expect(await install('2.0.0')).toEqual({ ok: true });
  // 原宿主可能要求reload；明确重开后再核真实激活版本，不把安装完成当作运行新版。
  await context.close(); context = undefined;
  await open();
  await inspect('2.0.0');
  result.phase = 'reject-node-only';
  const rejected = await install('node-only');
  expect(rejected.ok).toBe(false);
  expect(rejected.error).toContain('not a web extension');
  result.nodeOnlyRejected = rejected;
  result.phase = 'protected-profile';
  const original = (await profileText()).text;
  const installed = JSON.parse(original);
  expect(installed.map(item => [item.identifier.id, item.version])).toContainEqual(['boxteam-test.cache-probe', '2.0.0']);
  const corrupted = '{"unknownFormat":1}';
  await replaceProfile(corrupted);
  await command('workbench.action.closeActiveEditor');
  expect((await profileText()).text).toBe(corrupted);
  const failed = await install('1.0.0');
  expect(failed.ok).toBe(false);
  expect(failed.error).toMatch(/iterable|iterator/i);
  // 关闭原文本模型后再打开，确认读到持久原件而非dirty模型镜像。
  await command('workbench.action.closeActiveEditor');
  expect((await profileText()).text).toBe(corrupted);
  result.corruptProfilePreserved = true;
  await replaceProfile(original);
  await command('workbench.action.closeActiveEditor');
  expect((await profileText()).text).toBe(original);
  result.restoredProfile = true;
  await command('workbench.action.closeActiveEditor');
  result.phase = 'uninstall';
  await command('workbench.extensions.uninstallExtension', 'boxteam-test.cache-probe');
  expect(JSON.parse((await profileText()).text).some(item => item.identifier.id === 'boxteam-test.cache-probe')).toBe(false);
  await command('workbench.action.closeActiveEditor');
  await command('workbench.extensions.uninstallExtension', 'boxteam-test.cache-theme');
  expect(JSON.parse((await profileText()).text).some(item => item.identifier.id.startsWith('boxteam-test.cache-'))).toBe(false);
  result.uninstalled = true;
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = String(error);
  if (page && !page.isClosed()) await page.screenshot({ path: join(dirname(output), 'failure.png'), fullPage: true });
} finally {
  try { await run.close(); } catch (error) { failure ??= error; result.cleanupError = String(error); }
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
