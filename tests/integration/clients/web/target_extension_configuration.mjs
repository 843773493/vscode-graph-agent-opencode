import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { parse } from 'jsonc-parser';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_TARGET_EXTENSION_CONFIGURATION);
assert(fixture.artifacts && fixture.baseUrl && fixture.secondaryBaseUrl && fixture.owners,
  '必须显式提供正式产物目录、双 Gateway 及安装 owner');
const run = TestRunContext.fromTestFile('tests/integration/gateway/test_target_extension_configuration.py');
const section = 'targetConfigurationProbe';
const result = { phase: 'initialize', targets: {},
  boundary: '正式 Workbench／独立 Gateway 和受管 Backend／同字节真实 Node VSIX；公开 getConfiguration、inspect、Global 更新、语言覆盖及原 watcher。A/B 为同 BrowserContext 的不同站点标签页，关闭 Playwright 默认焦点模拟后真实切焦点；A2 使用独立 Context。安装装具保留显式模型回放，不代表完整 SettingsEditor 或 Windows 原生验收。' };
const save = () => writeFile(join(fixture.artifacts, 'target-configuration-browser.json'), JSON.stringify(result, null, 2) + '\n');
async function phase(value) { result.phase = value; await save(); }
const command = (target, name, payload) => target.page.evaluate(({ name, payload }) =>
  globalThis.workbench.commands.executeCommand('target-configuration-probe.' + name, payload), { name, payload });
const read = target => command(target, 'read');
const projection = snapshot => ({ base: snapshot.base, typescript: snapshot.typescript });
const values = (value, application, language, typescript = language) => ({ base: { value, application, language }, typescript });
const defaults = values('value-default', 'application-default', 'language-default');
const pages = [];
let failure;

function observe(target) {
  const { page, record } = target;
  page.on('pageerror', error => record.pageErrors.push(String(error.stack ?? error)));
  page.on('request', request => {
    if (new URL(request.url()).pathname !== '/api/gateway/extension-configurations/write') return;
    record.writes.push(request.postDataJSON());
  });
  page.on('websocket', socket => {
    if (new URL(socket.url()).pathname !== '/api/gateway/extension-hosts/channel') return;
    socket.on('framereceived', ({ payload }) => {
      if (typeof payload !== 'string') return;
      const binding = JSON.parse(payload);
      if (binding.initialization?.environment?.remote_settings) record.bindings.push(binding);
    });
  });
}

async function api(target, path, method = 'GET', payload) {
  return target.page.evaluate(async ({ path, method, payload, owner }) => {
    const credentialResponse = await fetch('/api/gateway/auth/local-credential');
    if (!credentialResponse.ok) throw new Error('本地认证失败：' + credentialResponse.status);
    const credential = await credentialResponse.json();
    const response = await fetch(path, { method, headers: {
      'Content-Type': 'application/json', 'X-Local-Token': credential.data.token,
      ...(owner && path.startsWith('/api/v1/') ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id,
      } : {}),
    }, ...(payload ? { body: JSON.stringify(payload) } : {}) });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || !body.request_id || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, body }));
    }
    return body;
  }, { path, method, payload, owner: target.owner });
}

async function openTarget(context, label, baseUrl, expectedOwner) {
  const record = result.targets[label] = { bindings: [], writes: [], pageErrors: [], snapshots: [] };
  const target = { page: await context.newPage(), record };
  pages.push({ label, page: target.page });
  // Playwright 默认让每个页面都拥有模拟焦点；关闭它后再核浏览器真实标签页切换。
  const focusSession = await context.newCDPSession(target.page);
  run.addCleanup('目标配置焦点 session ' + label, () => focusSession.detach());
  await focusSession.send('Emulation.setFocusEmulationEnabled', { enabled: false });
  observe(target);
  await target.page.goto(new URL('/api/gateway/health', baseUrl).href);
  await api(target, '/api/gateway/users/guest', 'POST', { tracking: { source: 'target-configuration-' + label } });
  const health = await api(target, '/api/gateway/health'), listing = await api(target, '/api/gateway/workspaces');
  target.owner = listing.data.active_workspace;
  assert.equal(target.owner.gateway_id, expectedOwner.gateway_id);
  assert.equal(target.owner.workspace_id, expectedOwner.workspace_id);
  assert.equal(health.data.gateway_id, target.owner.gateway_id);
  const workspace = await api(target, '/api/v1/workspace');
  assert.equal(workspace.data.workspace_id, target.owner.workspace_id);
  record.initialization = { health, listing, workspace };
  const roots = await api(target, '/api/v1/workspace/files/roots');
  const entry = new URL(baseUrl);
  entry.searchParams.set('folder', `boxteam-file://${target.owner.gateway_id}/${target.owner.workspace_id}/${encodeURIComponent(encodeURIComponent(roots.data.workspace_uri))}`);
  await target.page.goto(entry.href, { waitUntil: 'domcontentloaded' });
  const trust = target.page.getByRole('button', { name: /Yes, I trust the authors/ });
  if (await trust.isVisible()) await trust.click();
  await expect(target.page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await target.page.waitForFunction(() => !!globalThis.workbench?.commands);
  await expect.poll(async () => {
    try { return (await read(target)).extensionId; }
    catch (error) {
      if (!String(error).includes("command 'target-configuration-probe.read' not found")) throw error;
      return undefined;
    }
  }, { timeout: 30_000 }).toBe('boxteam-probe.target-configuration');
  await expect.poll(() => record.bindings.length, { timeout: 30_000 }).toBe(1);
  record.binding = record.bindings[0];
  assert.equal(record.binding.gateway_id, expectedOwner.gateway_id);
  assert.equal(record.binding.backend_id, expectedOwner.backend_id);
  assert.deepEqual(record.binding.workspace_ids, [expectedOwner.workspace_id]);
  return target;
}

async function waitForValues(target, expected, label) {
  await expect.poll(async () => projection(await read(target)), { timeout: 20_000 }).toEqual(expected);
  const snapshot = await read(target);
  target.record.snapshots.push({ label, ...snapshot });
  for (const [key, value] of Object.entries(expected.base)) {
    const inspected = snapshot.inspect[key];
    assert.equal(inspected.globalLocalValue, null, key + ' 不得读取窗口 profile 的扩展覆盖');
    assert.equal(inspected.globalLocalLanguageValue, null);
    const globalValue = value === defaults.base[key] ? null : value;
    assert.equal(inspected.globalRemoteValue, globalValue);
    assert.equal(inspected.globalValue, globalValue);
    for (const field of ['workspaceValue', 'workspaceFolderValue', 'workspaceLanguageValue', 'workspaceFolderLanguageValue']) {
      assert.equal(inspected[field], null, key + ' 的 Global 写入不能进入 ' + field);
    }
  }
  const language = snapshot.typescriptInspect;
  assert.equal(language.globalRemoteValue, expected.base.language === defaults.base.language ? null : expected.base.language);
  assert.equal(language.globalLocalValue, null);
  assert.equal(language.globalLocalLanguageValue, null);
  const override = expected.typescript === expected.base.language ? null : expected.typescript;
  assert.equal(language.globalRemoteLanguageValue, override);
  assert.equal(language.globalLanguageValue, override);
  return snapshot;
}

async function update(target, key, value, extra = {}) {
  await command(target, 'update', { key, value, ...extra });
}
async function settings(target) {
  const binding = target.record.binding;
  const { protocol_version, gateway_id, backend_id, view_id, generation, workspace_ids } = binding;
  const response = await api(target, '/api/gateway/extension-configurations/read', 'POST', {
    protocol_version, gateway_id, backend_id, view_id, generation, workspace_ids,
    resource_uri: binding.initialization.environment.remote_settings.resource_uri,
  });
  return response.data;
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('目标配置 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('目标配置 A/B context', () => context.close());
  // Gateway cookie 按站点隔离；端口自身不能隔离同名 cookie。
  const secondaryUrl = new URL(fixture.secondaryBaseUrl);
  assert.equal(secondaryUrl.hostname, '127.0.0.1');
  secondaryUrl.hostname = 'localhost';
  const a = await openTarget(context, 'a', fixture.baseUrl, fixture.owners.a);
  const b = await openTarget(context, 'b', secondaryUrl.href, fixture.owners.b);
  await waitForValues(a, defaults, 'default-a');
  await waitForValues(b, defaults, 'default-b');
  assert.notEqual(a.record.binding.generation, b.record.binding.generation);
  assert.notEqual(a.record.binding.initialization.environment.remote_settings.resource_uri,
    b.record.binding.initialization.environment.remote_settings.resource_uri);
  await phase('global-and-language-through-public-api');
  await update(a, 'value', 'A-global');
  await update(a, 'application', 'A-application');
  await update(a, 'language', 'A-language-base');
  await update(a, 'language', 'A-typescript-explicit', { languageId: 'typescript', scopeToLanguage: true });
  const initialA = values('A-global', 'A-application', 'A-language-base', 'A-typescript-explicit');
  await waitForValues(a, initialA, 'a-explicit-language');
  await waitForValues(b, defaults, 'b-unaffected-by-a');
  await command(a, 'update-block', { [section + '.language']: 'A-typescript-block' });
  await waitForValues(a, { ...initialA, typescript: 'A-typescript-block' }, 'a-extension-only-language-block');
  await command(a, 'update-block', {});
  await waitForValues(a, { ...initialA, typescript: initialA.base.language }, 'a-empty-language-block');
  const emptyBlock = await settings(a), writesBeforeDelete = a.record.writes.length;
  assert.deepEqual(parse(Buffer.from(emptyBlock.content_base64, 'base64').toString('utf8'))['[typescript]'], {});
  await command(a, 'update-block');
  const removedBlock = await settings(a);
  assert.equal(Object.hasOwn(parse(Buffer.from(removedBlock.content_base64, 'base64').toString('utf8')), '[typescript]'), false);
  assert(a.record.writes.length > writesBeforeDelete, '删除目标空语言块必须触发真实文件写入');
  await waitForValues(a, { ...initialA, typescript: initialA.base.language }, 'a-empty-language-block-deleted');
  a.record.emptyLanguageBlockDeleted = { empty: emptyBlock, removed: removedBlock };
  await command(a, 'update-block', { [section + '.language']: 'A-typescript-block' });
  await waitForValues(a, { ...initialA, typescript: 'A-typescript-block' }, 'a-language-block-restored');
  const beforeMixed = await settings(a), writesBeforeMixed = a.record.writes.length;
  await assert.rejects(() => command(a, 'update-block', { [section + '.language']: 'must-not-save', 'editor.fontSize': 42 }),
    /语言覆盖同时包含窗口与目标扩展设置/);
  assert.equal(a.record.writes.length, writesBeforeMixed, '混合域语言块必须在 FileService 写入前拒绝');
  assert.deepEqual(await settings(a), beforeMixed, '混合域语言块拒绝后原始设置字节必须不变');
  a.record.mixedDomainRejectedWithoutWrite = true;
  await phase('same-target-independent-window');
  const secondContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('目标配置 A2 context', () => secondContext.close());
  const a2 = await openTarget(secondContext, 'a2', fixture.baseUrl, fixture.owners.a);
  assert.equal(a2.record.binding.initialization.environment.profile_id, a.record.binding.initialization.environment.profile_id);
  assert.deepEqual(a2.record.binding.initialization.environment.remote_settings, a.record.binding.initialization.environment.remote_settings);
  assert.notEqual(a2.record.binding.client_id, a.record.binding.client_id);
  assert.notEqual(a2.record.binding.generation, a.record.binding.generation);
  await waitForValues(a2, { ...initialA, typescript: 'A-typescript-block' }, 'a2-reads-target-user');
  for (const [key, value] of Object.entries({ value: 'B-global', application: 'B-application', language: 'B-language-base' })) {
    await update(b, key, value);
  }
  await update(b, 'language', 'B-typescript', { languageId: 'typescript', scopeToLanguage: true });
  const expectedB = values('B-global', 'B-application', 'B-language-base', 'B-typescript');
  await waitForValues(b, expectedB, 'b-independent-user-settings');
  b.record.beforeForeignWrites = await settings(b);
  await phase('focus-b-and-write-original-a');
  await b.page.bringToFront();
  await expect.poll(() => b.page.evaluate(() => document.hasFocus())).toBe(true);
  result.focus = { active: 'b', a: await a.page.evaluate(() => document.hasFocus()), b: true };
  assert.equal(result.focus.a, false);
  const a2EventsBefore = (await read(a2)).events.length;
  await update(a, 'value', 'A-after-B-focus');
  // 省略 scopeToLanguage 时，原 MainThread 必须沿目标 userRemote 中已有的语言覆盖推导。
  await update(a, 'language', 'A-typescript-implicit', { languageId: 'typescript' });
  const afterFocusA = values('A-after-B-focus', 'A-application', 'A-language-base', 'A-typescript-implicit');
  await waitForValues(a, afterFocusA, 'a-writes-while-b-focused');
  await waitForValues(a2, afterFocusA, 'a2-observes-original-a-via-watcher');
  assert((await read(a2)).events.length > a2EventsBefore, '另一 A 窗口必须收到真实配置变化');
  await waitForValues(b, expectedB, 'b-keeps-own-values-after-a-write');
  assert.deepEqual(await settings(b), b.record.beforeForeignWrites);
  await phase('second-window-writes-same-target');
  await b.page.bringToFront();
  await expect.poll(() => b.page.evaluate(() => document.hasFocus())).toBe(true);
  const aEventsBefore = (await read(a)).events.length;
  await update(a2, 'application', 'A-second-window');
  const finalA = values('A-after-B-focus', 'A-second-window', 'A-language-base', 'A-typescript-implicit');
  await waitForValues(a2, finalA, 'a2-writes-while-b-focused');
  await waitForValues(a, finalA, 'a-observes-second-window-via-watcher');
  assert((await read(a)).events.length > aEventsBefore, '原 A 窗口必须收到第二窗口配置变化');
  await waitForValues(b, expectedB, 'b-keeps-own-values-after-a2-write');
  a.record.finalSettings = await settings(a);
  a2.record.finalSettings = await settings(a2);
  b.record.finalSettings = await settings(b);
  assert.deepEqual(a.record.finalSettings, a2.record.finalSettings);
  assert.deepEqual(b.record.finalSettings, b.record.beforeForeignWrites);
  for (const target of [a, a2, b]) {
    assert.deepEqual(target.record.pageErrors, []);
    const binding = target.record.binding, resource = binding.initialization.environment.remote_settings.resource_uri;
    for (const body of target.record.writes) {
      assert.equal(body.gateway_id, binding.gateway_id); assert.equal(body.backend_id, binding.backend_id);
      assert.equal(body.resource_uri, resource); assert.equal(body.generation, binding.generation);
    }
  }
  await a.page.screenshot({ path: join(fixture.artifacts, 'target-configuration-a.png'), fullPage: true });
  await phase('complete');
} catch (error) {
  failure = error;
  result.error = String(error.stack ?? error);
  const diagnostics = await Promise.allSettled(pages.map(({ label, page }) =>
    page.screenshot({ path: join(fixture.artifacts, 'target-configuration-failure-' + label + '.png'), fullPage: true })));
  const errors = diagnostics.filter(item => item.status === 'rejected').map(item => item.reason);
  if (errors.length) failure = new AggregateError([failure, ...errors], '配置验收及截图失败');
} finally {
  try { await save(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '配置验收及证据保存失败') : error; }
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '配置验收及清理失败') : error; }
}
if (failure) throw failure;
