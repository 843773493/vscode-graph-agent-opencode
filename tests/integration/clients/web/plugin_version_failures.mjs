import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const input = JSON.parse(process.env.BOXTEAM_PLUGIN_VERSION_FAILURE_FIXTURE);
const result = { phase: 'initialization', pageErrors: [], externalRequests: [], receipts: [], exitedPids: [] };
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== new URL(input.baseUrl).origin) result.externalRequests.push(request.url());
  });
  const api = (path, options = {}) => page.evaluate(async ({ path, options }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json' }, body: options.body && JSON.stringify(options.body) });
    const envelope = await response.json();
    if (envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(`request_id 不一致: ${path}`);
    return { status: response.status, ...envelope };
  }, { path, options });
  const read = async (path, options) => {
    const receipt = await api(path, options); assert.equal(receipt.status, 200, JSON.stringify(receipt));
    result.receipts.push({ path, ...receipt }); return receipt.data;
  };
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const health = await read('/api/gateway/health'), workspaces = await read('/api/gateway/workspaces');
  await read('/api/v1/workspace');
  assert.equal(workspaces.active_workspace.gateway_id, health.gateway_id);
  const target = { gateway_id: health.gateway_id };
  const status = async () => (await read('/api/gateway/service-plugins/status', { method: 'POST', body: target })).plugins
    .find(item => item.binding.package_id === 'proof.version-failures');
  const source = () => read('/api/gateway/config/sources/gateway_mutable_override');
  const starts = async () => (await readFile(input.attempts, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
  const view = page.locator('.service-plugin-packages-view'), quick = page.locator('.quick-input-widget');
  const pick = async label => { await expect(quick).toBeVisible(); await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click(); };
  for (let i = 0; i < input.packages.length; i++) {
    await view.getByRole('button', { name: '安装插件包', exact: true }).click(); await pick('ZIP 文件');
    const field = quick.locator('input').first(); await field.fill(input.packages[i]); await field.press('Enter');
    await expect(view.locator('.service-plugin-package')).toHaveCount(i + 1);
  }
  const row = version => view.locator('.service-plugin-package').filter({
    has: page.getByRole('heading', { name: `换版失败探针包 · proof.version-failures@${version}`, exact: true }),
  });
  const open = async version => { await row(version).getByRole('button', { name: '运行探针 · probe', exact: true }).click(); };
  const apply = async (version, action) => {
    await open(version); await view.getByRole('button', { name: action, exact: true }).click();
    await pick('gateway_mutable_override');
    const saved = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname.endsWith('/config/sources/gateway_mutable_override'));
    await pick('确认保存 Gateway 插件绑定');
    const response = await saved; assert.equal(response.status(), 200);
    const receipt = await response.json();
    assert.equal(receipt.request_id, response.headers()['x-request-id']);
    assert.equal(receipt.data.edit.source_saved, true); assert.equal(receipt.data.edit.state, 'active');
    result.receipts.push({ action, version, ...receipt });
    await expect(view.getByLabel('插件配置与运行版本')).toContainText(`配置：启用 @${version}`);
  };
  const call = item => api(`/api/gateway/service-plugins/${item.binding.package_id}/probe/instances/${item.instance.instance_id}/services/echo/value?package_version=${item.instance.package_version}`);
  const restart = async version => {
    await open(version);
    const completed = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith('/service-plugins/restart'));
    await view.getByRole('button', { name: '重启实例', exact: true }).click();
    const response = await completed; assert.equal(response.status(), 200);
    const receipt = await response.json(); assert.equal(receipt.request_id, response.headers()['x-request-id']);
    result.receipts.push({ restart: version, ...receipt });
    await expect(view.getByLabel('插件配置与运行版本')).toContainText(`实际：ready @${version}`);
    return status();
  };
  await apply('1.0.0', '启用此范围');
  const original = await status(); assert.equal(original.state, 'ready');
  assert.equal((await call(original)).version, '1.0.0');
  const installed = await starts(); assert.equal(installed.length, 1);

  result.phase = 'start-failure';
  await apply('2.0.0', '切换到此版本（保留开关）');
  await expect(view.getByLabel('插件运行失败原因')).toContainText('启动实例失败: 插件在 ready 前退出: exit_code=23');
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('实际：failed · 尚无 ready 实例回执');
  const failed = await status(); assert.equal(failed.binding.package_version, '2.0.0'); assert.equal(failed.binding.enabled, true);
  assert.equal(failed.instance, null); assert.equal((await call(original)).status, 409);
  assert.equal((await source()).source_effective.plugins.runtime.entries[0].package_version, '2.0.0');
  for (let i = 0; i < 2; i++) {
    await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.extensions.action.refreshExtension'));
    await expect(view.getByLabel('插件运行失败原因')).toContainText('启动实例失败');
  }
  assert.deepEqual((await starts()).map(item => item.version), ['1.0.0', '2.0.0']);
  result.exitedPids.push(original.instance.process_id, (await starts())[1].pid);
  result.startFailure = true;
  const recovered = await restart('2.0.0');
  assert.equal(recovered.binding.package_version, '2.0.0'); assert.equal(recovered.error, null);
  assert.notEqual(recovered.instance.instance_id, original.instance.instance_id);
  assert.equal((await call(recovered)).version, '2.0.0'); assert.equal((await starts()).length, 3);

  result.phase = 'stop-failure';
  await apply('3.0.0', '切换到此版本（保留开关）');
  const beforeStop = await status(); assert.equal(beforeStop.state, 'ready');
  result.exitedPids.push(recovered.instance.process_id);
  await apply('1.0.0', '切换到此版本（保留开关）');
  await expect(view.getByLabel('插件运行失败原因')).toContainText('停止原实例失败（原运行版本 3.0.0；未启动新实例）: 插件 shutdown 回执失败；原进程已核实退出');
  await expect(view.getByLabel('插件配置与运行版本')).toContainText('实际：failed @3.0.0');
  const stopped = await status(); assert.equal(stopped.binding.package_version, '1.0.0'); assert.equal(stopped.binding.enabled, true);
  assert.deepEqual(stopped.instance, beforeStop.instance);
  assert.equal((await call(beforeStop)).status, 409); assert.equal((await starts()).length, 4);
  assert.equal((await source()).source_effective.plugins.runtime.entries[0].package_version, '1.0.0');
  result.exitedPids.push(beforeStop.instance.process_id);
  result.stopFailure = true;
  const final = await restart('1.0.0');
  assert.equal(final.error, null); assert.equal((await call(final)).version, '1.0.0');
  assert.equal((await starts()).length, 5); result.explicitRecovery = true;
  result.phase = 'complete';
  assert.deepEqual(result.pageErrors, []); assert.deepEqual(result.externalRequests, []);
} catch (error) {
  result.error = String(error); result.stack = error.stack;
  if (page) await page.screenshot({ path: input.resultPath.replace(/\.json$/, '.png'), fullPage: true }).catch(screenshotError => { result.screenshotError = String(screenshotError); });
  throw error;
} finally {
  await writeFile(input.resultPath, JSON.stringify(result, null, 2) + '\n');
  await browser?.close();
}
