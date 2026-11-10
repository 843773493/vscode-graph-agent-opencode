import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const input = JSON.parse(process.env.BOXTEAM_PLUGIN_UI_FIXTURE);
const result = { phase: 'initialization', pageErrors: [], writes: [], queries: [], confirmations: [], results: [] };
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
let page, releaseWrite;
try {
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname.includes('/config/sources/')) {
      result.writes.push({ path: url.pathname, body: JSON.parse(request.postData()), headers: request.headers() });
    }
    if (url.searchParams.has('idempotency_key')) result.queries.push({ path: url.pathname, key: url.searchParams.get('idempotency_key') });
  });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  const api = (path, options = {}) => page.evaluate(async ({ path, options }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': credential.data.token,
      'Content-Type': 'application/json', ...(options.headers ?? {}) }, body: options.body && JSON.stringify(options.body) });
    const value = await response.json();
    if (!response.ok || value.request_id !== response.headers.get('X-Request-ID')) throw new Error(`${path}: ${response.status} ${JSON.stringify(value)}`);
    return value.data;
  }, { path, options });
  result.initialization = [];
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push(await api(path));
  const target = result.initialization[1].active_workspace;
  assert.equal(target.gateway_id, result.initialization[0].gateway_id);
  const workspace = result.initialization[1].items.find(item => item.gateway_id === target.gateway_id && item.workspace_id === target.workspace_id);
  assert(workspace);
  result.owner = target;
  const pluginTarget = { gateway_id: target.gateway_id };
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.view.extensions'));
  const view = page.locator('.service-plugin-packages-view'), quick = page.locator('.quick-input-widget');
  const dialog = page.locator('.monaco-dialog-box');
  await expect(view).toBeVisible();
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.getByRole('option').filter({ hasText: label }).first().click();
  };
  await view.getByRole('button', { name: '安装插件包', exact: true }).click();
  await pick('ZIP 文件');
  await quick.locator('input').fill(input.packagePath);
  await page.keyboard.press('Enter');
  await expect(view).toContainText('ui.preference-probe@1.0.0', { timeout: 30_000 });

  const beginBatch = async (enabled, included, excluded) => {
    await view.getByRole('button', { name: enabled ? '全部启用此范围' : '全部停用此范围', exact: true }).click();
    await expect(dialog).toBeVisible();
    const text = await dialog.innerText();
    assert(text.includes(`纳入：${included}`), text);
    assert(text.includes(`排除（固定范围不匹配）：${excluded}`), text);
    result.confirmations.push(text);
  };
  const confirmBatch = async (enabled, source) => {
    await dialog.getByRole('button', { name: enabled ? '全部启用此范围' : '全部停用此范围', exact: true }).click();
    await pick(source);
  };
  const readResults = async (scope, ids) => {
    await expect(dialog).toContainText('插件批量操作结果');
    const text = await dialog.innerText();
    assert(text.includes(`Gateway ${target.gateway_id} · ${scope}`), text);
    for (const id of ids) assert(text.includes(`ui.preference-probe/${id}@1.0.0`), text);
    result.results.push(text);
    await dialog.locator('.dialog-buttons .monaco-button').first().click();
    await expect(dialog).toBeHidden();
    return text;
  };
  result.phase = 'cancel';
  const count = result.writes.length;
  await beginBatch(true, 'gateway-preference', 'workspace-preference（workspace）、workspace-required（workspace）');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  assert.equal(result.writes.length, count);
  result.cancelledWithoutWrite = true;

  result.phase = 'gateway';
  await beginBatch(true, 'gateway-preference', 'workspace-preference（workspace）、workspace-required（workspace）');
  await confirmBatch(true, 'gateway_mutable_override');
  const gatewayResult = await readResults('gateway', ['gateway-preference']);
  assert(gatewayResult.includes('active'), gatewayResult);
  const gatewayPath = '/api/gateway/config/sources/gateway_mutable_override';
  const gatewayBefore = await api(gatewayPath);
  assert.equal(gatewayBefore.effective.plugins.runtime.entries[0].enabled, true);

  result.phase = 'defaults';
  await view.getByLabel('插件管理范围').selectOption('defaults');
  await expect(view).toContainText('workspace-required');
  const runtimeBefore = await api('/api/gateway/service-plugins/status', { method: 'POST', body: pluginTarget });
  await beginBatch(false, 'workspace-preference、workspace-required', 'gateway-preference（gateway）');
  await confirmBatch(false, 'gateway_mutable_override');
  const defaultsResult = await readResults('defaults', ['workspace-preference', 'workspace-required']);
  assert(defaultsResult.includes('仅修改新建默认值，未操作当前实例'));
  const defaultsDocument = await api(gatewayPath);
  const defaultsBefore = defaultsDocument.effective.plugins.workspace_defaults.entries;
  assert.deepEqual(defaultsBefore.map(item => [item.plugin_id, item.enabled]), [['workspace-preference', false], ['workspace-required', false]]);
  const templateRuntime = await api('/api/gateway/service-plugins/status', { method: 'POST', body: pluginTarget });
  // revision 标识已接收的配置快照；模板保存可更新它，绑定、实例和实际状态必须保持。
  const runtimeFacts = slots => slots.map(({ revision, ...facts }) => facts);
  assert.deepEqual(runtimeFacts(templateRuntime.plugins), runtimeFacts(runtimeBefore.plugins));
  assert(templateRuntime.plugins.every(slot => slot.revision === defaultsDocument.active_digest));
  result.defaultsOnly = true;

  result.phase = 'workspace-partial-and-lost-response';
  await view.getByLabel('插件管理范围').selectOption('workspace');
  await pick(workspace.name);
  await expect(view).toContainText('workspace-required');
  let submitted;
  const committed = new Promise(resolve => { submitted = resolve; });
  const release = new Promise(resolve => { releaseWrite = resolve; });
  let dropped = false;
  await page.route('**/api/v1/config/sources/workspace_root_mutable_override', async route => {
    if (route.request().method() !== 'PUT' || dropped) { await route.continue(); return; }
    dropped = true;
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    const receipt = await response.json();
    assert.equal(receipt.data.edit.state, 'active');
    result.originalKey = JSON.parse(route.request().postData()).idempotency_key;
    submitted();
    await release;
    await route.abort('failed');
  });
  await beginBatch(true, 'workspace-preference、workspace-required', 'gateway-preference（gateway）');
  await confirmBatch(true, 'workspace_root_mutable_override');
  await committed;
  // 仅延迟并丢弃真实提交的响应；切换列表不能改变在途批量操作的 owner。
  await view.getByLabel('插件管理范围').selectOption('gateway');
  await expect(view).toContainText('gateway-preference');
  releaseWrite();
  const workspaceResult = await readResults('workspace', ['workspace-preference', 'workspace-required']);
  assert(workspaceResult.includes(`工作区 ${target.workspace_id}`), workspaceResult);
  assert(workspaceResult.includes('active') && workspaceResult.includes('未提交'), workspaceResult);
  assert(workspaceResult.includes('required'), workspaceResult);
  const workspaceWrites = result.writes.filter(write => write.path === '/api/v1/config/sources/workspace_root_mutable_override');
  assert.equal(workspaceWrites.length, 1, '响应丢失只查询原提交，失败插件不能写入或重放成功项');
  assert.equal(workspaceWrites[0].headers['x-boxteam-workspace-id'], target.workspace_id);
  assert(result.queries.some(query => query.key === result.originalKey));
  await page.unroute('**/api/v1/config/sources/workspace_root_mutable_override');
  const source = await api(`/api/v1/workspaces/${target.workspace_id}/config/sources/workspace_root_mutable_override`,
    { headers: { 'X-BoxTeam-Workspace-Id': target.workspace_id } });
  assert.deepEqual(source.effective.plugins.runtime.entries.map(item => [item.plugin_id, item.enabled]), [['workspace-preference', true]]);
  const gatewayAfter = await api(gatewayPath);
  assert.deepEqual(gatewayAfter.effective.plugins.runtime, gatewayBefore.effective.plugins.runtime);
  assert.deepEqual(gatewayAfter.effective.plugins.workspace_defaults.entries, defaultsBefore);
  result.targetFrozen = result.partialFailure = result.lostResponseQueried = true;

  result.phase = 'gateway-disable';
  await beginBatch(false, 'gateway-preference', 'workspace-preference（workspace）、workspace-required（workspace）');
  await confirmBatch(false, 'gateway_mutable_override');
  await readResults('gateway', ['gateway-preference']);
  assert.equal((await api(gatewayPath)).effective.plugins.runtime.entries[0].enabled, false);
  result.phase = 'complete';
} catch (error) {
  result.failure = error instanceof Error ? error.stack : String(error);
  if (page) await page.screenshot({ path: `${input.artifacts}/plugin-batch-failure.png` }).catch(screenshotError => { result.screenshotError = String(screenshotError); });
  throw error;
} finally {
  releaseWrite?.();
  await writeFile(input.resultPath, `${JSON.stringify(result, null, 2)}\n`);
  await browser.close();
}
