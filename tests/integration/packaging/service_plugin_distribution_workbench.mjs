import assert from 'node:assert/strict';
import { once } from 'node:events';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// 驱动也只用发行物的 Node、Playwright 和 Chromium，不借开发依赖。
const input = JSON.parse(process.env.BOXTEAM_DISTRIBUTION_WORKBENCH);
const { chromium, _electron, expect } = await import(pathToFileURL(path.join(input.application, 'node_modules/playwright/test.mjs')).href);
const result = { phase: 'initialize', pageErrors: [], externalRequests: [], exitedPids: [], calls: [],
  native: { launches: [], exits: [], storageErrors: [] } };
let browser, nativeApp, page;
const observePage = next => {
  page = next;
  page.setDefaultTimeout(20_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && /IndexedDB|database.*closed/i.test(message.text())) {
      result.native.storageErrors.push(message.text());
    }
  });
  page.on('dialog', dialog => dialog.type() === 'beforeunload' ? dialog.accept() : dialog.dismiss());
  page.on('request', request => {
    const url = new URL(request.url());
    const document = new URL(next.url());
    if (['http:', 'https:'].includes(url.protocol) && document.protocol === 'http:' && url.origin !== document.origin) {
      result.externalRequests.push(request.url());
    }
  });
};
const openNative = async () => {
  nativeApp = await _electron.launch({ executablePath: input.electron,
    args: ['--no-sandbox', `--gateway=${input.baseUrl}`], cwd: input.cwd,
    env: { ...process.env, BOXTEAM_HOME: input.profile, PATH: input.emptyPath }, timeout: 60_000 });
  nativeApp.process().stdout.on('data', chunk => process.stdout.write(chunk));
  nativeApp.process().stderr.on('data', chunk => process.stderr.write(chunk));
  const location = await nativeApp.evaluate(({ app }) => ({ packaged: app.isPackaged,
    appPath: app.getAppPath(), cwd: process.cwd(), path: process.env.PATH, userData: app.getPath('userData') }));
  assert.equal(location.packaged, true);
  assert.ok(location.appPath.endsWith('/resources/app.asar'));
  assert.equal(location.cwd, input.cwd); assert.equal(location.path, input.emptyPath);
  assert.equal(location.userData, path.join(input.profile, 'clients/electron'));
  result.native.launches.push(location);
  observePage(await nativeApp.firstWindow());
  await page.waitForURL(url => url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.pathname === '/',
    { waitUntil: 'domcontentloaded' });
  assert.deepEqual(await page.evaluate(() => [typeof globalThis.require, typeof globalThis.process]), ['undefined', 'undefined']);
};
const closeNative = async () => {
  if (!nativeApp) return;
  const exited = once(nativeApp.process(), 'exit', { signal: AbortSignal.timeout(30_000) });
  await nativeApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  const [code, signal] = await exited;
  result.native.exits.push({ code, signal });
  nativeApp = undefined;
  assert.equal(code, 0); assert.equal(signal, null);
  assert.deepEqual(result.native.storageErrors, []);
};
try {
  if (input.electron) await openNative();
  else {
    browser = await chromium.launch({ executablePath: input.chromium, headless: true });
    observePage(await browser.newPage({ viewport: { width: 1500, height: 950 } }));
    await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  }
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const api = (url, body, raw = false) => page.evaluate(async ({ url, body, raw }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(url, { method: body ? 'POST' : 'GET',
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(`${url}: ${response.status} ${JSON.stringify(payload)}`);
    }
    return raw ? payload : payload.data;
  }, { url, body, raw });
  result.initialization = [];
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    result.initialization.push({ url, data: await api(url) });
  }
  const active = result.initialization[1].data.active_workspace;
  assert.equal(active.gateway_id, result.initialization[0].data.gateway_id);
  assert.equal(active.workspace_id, result.initialization[2].data.workspace_id);
  const target = { gateway_id: active.gateway_id };
  const slots = async () => Object.fromEntries((await api('/api/gateway/service-plugins/status', target)).plugins
    .filter(item => item.binding.package_id === 'distribution.probe').map(item => [item.binding.plugin_id, item]));
  const call = async slot => {
    assert.equal(slot.state, 'ready');
    const value = await api(`/api/gateway/service-plugins/distribution.probe/${slot.binding.plugin_id}/instances/`
      + `${slot.instance.instance_id}/services/echo/value?package_version=${slot.binding.package_version}`, undefined, true);
    assert.equal(value.instance_id, slot.instance.instance_id);
    assert.equal(value.pid, slot.instance.process_id);
    assert.equal(value.version, slot.binding.package_version);
    result.calls.push(value);
  };
  const openManagement = async () => {
    await page.waitForFunction(() => typeof globalThis.workbench?.commands?.executeCommand === 'function');
    await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
    await expect(page.locator('.service-plugin-packages-view')).toBeVisible();
  };
  await openManagement();
  let view = page.locator('.service-plugin-packages-view');
  let quick = page.locator('.quick-input-widget');
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click();
  };
  const row = version => view.locator('.service-plugin-package').filter({
    has: page.getByRole('heading', { name: `发行闭环探针 · distribution.probe@${version}`, exact: true }),
  });
  const install = async (version, file) => {
    await view.getByRole('button', { name: '安装插件包', exact: true }).click();
    await pick('ZIP 文件');
    const field = quick.locator('input').first();
    await field.fill(file); await field.press('Enter');
    await expect(row(version)).toBeVisible({ timeout: 30_000 });
  };
  const open = async (version, plugin) => {
    await row(version).getByRole('button', { name: `${plugin} · ${plugin}`, exact: true }).click();
    await expect(view.locator('.service-plugin-details')).toBeVisible();
  };
  const saveBinding = async (action, expected) => {
    await view.getByRole('button', { name: action, exact: true }).click();
    await pick('gateway_mutable_override');
    await pick('确认保存 Gateway 插件绑定');
    await expect(quick).not.toBeVisible();
    await expect(view.getByLabel('插件配置与运行版本')).toContainText(expected);
  };
  result.phase = 'install-and-bind';
  await install('1.0.0', input.packages[0]);
  assert.deepEqual(await slots(), {});
  for (const plugin of ['python', 'node']) {
    await open('1.0.0', plugin); await saveBinding('启用此范围', '配置：启用 @1.0.0');
    await expect.poll(async () => (await slots())[plugin]?.state, { timeout: 20_000 }).toBe('ready');
    await call((await slots())[plugin]);
  }
  const original = await slots();
  assert.notEqual(original.python.instance.process_id, original.node.instance.process_id);
  if (input.electron) { await closeNative(); await openNative(); }
  else await page.reload({ waitUntil: 'domcontentloaded' });
  await openManagement();
  view = page.locator('.service-plugin-packages-view');
  quick = page.locator('.quick-input-widget');
  assert.deepEqual(await slots(), original);
  result.phase = 'update-and-switch';
  await install('2.0.0', input.packages[1]);
  assert.deepEqual(await slots(), original);
  for (const plugin of ['python', 'node']) {
    const before = await slots();
    await open('2.0.0', plugin); await saveBinding('切换到此版本（保留开关）', '配置：启用 @2.0.0');
    const current = await slots();
    assert.equal(current[plugin].binding.package_version, '2.0.0');
    assert.notEqual(current[plugin].instance.instance_id, before[plugin].instance.instance_id);
    const sibling = plugin === 'python' ? 'node' : 'python';
    assert.deepEqual(current[sibling].instance, before[sibling].instance);
    result.exitedPids.push(before[plugin].instance.process_id);
    await call(current[plugin]);
  }
  result.phase = 'restart';
  const beforeRestart = await slots();
  await open('2.0.0', 'python');
  const restarted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/restart');
  await view.getByRole('button', { name: '重启实例', exact: true }).click();
  assert.equal((await restarted).status(), 200);
  const current = await slots();
  assert.notEqual(current.python.instance.instance_id, beforeRestart.python.instance.instance_id);
  assert.deepEqual(current.node.instance, beforeRestart.node.instance);
  result.exitedPids.push(beforeRestart.python.instance.process_id);
  await call(current.python);
  result.phase = 'disable-and-uninstall';
  for (const plugin of ['python', 'node']) {
    await open('2.0.0', plugin); await saveBinding('停用此范围', '配置：停用 @2.0.0');
    assert.equal((await slots())[plugin].state, 'stopped');
    result.exitedPids.push(current[plugin].instance.process_id);
  }
  const blocked = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/gateway/service-plugins/uninstall');
  await row('2.0.0').getByRole('button', { name: '卸载整个包', exact: true }).click(); await pick('卸载整包');
  assert.equal((await blocked).status(), 409);
  await expect(page.locator('.notifications-toasts')).toContainText('configuration:');
  for (const plugin of ['python', 'node']) {
    await open('2.0.0', plugin); await saveBinding('解除此范围绑定', '配置：未绑定');
  }
  assert.deepEqual(await slots(), {});
  for (const version of ['1.0.0', '2.0.0']) {
    await row(version).getByRole('button', { name: '卸载整个包', exact: true }).click(); await pick('卸载整包');
    await expect(row(version)).toHaveCount(0);
  }
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.externalRequests, []);
  assert.deepEqual(result.native.storageErrors, []);
  result.phase = 'complete';
  await page.screenshot({ path: input.screenshot, fullPage: true });
} catch (error) {
  result.error = String(error?.stack ?? error);
  throw error;
} finally {
  try { await closeNative(); await browser?.close(); }
  finally { await writeFile(input.resultPath, JSON.stringify(result, null, 2) + '\n'); }
}
