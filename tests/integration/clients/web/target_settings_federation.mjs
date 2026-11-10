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
  boundary: '正式 Workbench、真实双 Gateway/Backend、原安装器与 Web Worker；A 经 Relay 写 B 原宿主配置，B Worker 公开配置 API 回写，A 经同一文件 owner 读取；不验证已退役的 profile/view 设置 UI。SSH/隧道是具名 loopback 装具，不代替真实 SSH、Windows 或 Electron。' };
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
  await phase('original-host-profile-transport');
  // 这里只验证仍在使用的宿主配置 API；业务设置表单由正式来源验收覆盖。
  const prepared = await api(a, '/api/gateway/extension-configurations/prepare', 'POST', {
    protocol_version: 1, ...fixture.targets.b, workspace_ids: [fixture.workspace_id],
  });
  result.binding = prepared;
  assert.equal(prepared.gateway_id, fixture.targets.b.gateway_id);
  assert.equal(prepared.backend_id, fixture.targets.b.backend_id);
  const resource = prepared.context.remote_settings.resource_uri;
  const target = { protocol_version: 1, gateway_id: prepared.gateway_id, backend_id: prepared.backend_id,
    view_id: prepared.view_id, workspace_ids: prepared.workspace_ids, profile_id: prepared.profile_id, resource_uri: resource };
  await phase('relay-writes-B-worker-reads');
  await api(a, '/api/gateway/extension-configurations/write', 'POST', { ...target,
    content_base64: Buffer.from(JSON.stringify({ 'targetSettingsProbe.value': 'from-A-API' })).toString('base64'),
    options: { create: true, overwrite: true, unlock: false, atomic: false, append: false } });
  await expect.poll(async () => (await read(b)).value, { timeout: 20_000 }).toBe('from-A-API');
  const remote = await read(b);
  assert(remote.worker); assert.equal(remote.inspect.globalValue, 'from-A-API');
  assert.equal(remote.inspect.globalRemoteValue, 'from-A-API');
  assert.equal(remote.inspect.globalLocalValue, undefined);
  assert(remote.events.includes('from-A-API'));
  result.afterRelayWrite = remote;
  result.localValue = (await read(a)).value;
  assert.equal(result.localValue, 'target-default');
  await phase('worker-writes-relay-reads');
  const updated = await query(b, 'boxteam.settingsProbe.update', 'from-B-worker');
  assert.equal(updated.value, 'from-B-worker');
  const body = await api(a, '/api/gateway/extension-configurations/read', 'POST', target);
  const values = JSON.parse(Buffer.from(body.content_base64, 'base64').toString('utf8'));
  assert.equal(values['targetSettingsProbe.value'], 'from-B-worker');
  assert.equal((await read(a)).value, 'target-default');
  result.relayValue = values['targetSettingsProbe.value']; result.remoteValue = (await read(b)).value;
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
