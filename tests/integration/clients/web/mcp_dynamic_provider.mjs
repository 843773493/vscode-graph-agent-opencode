import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_MCP_DYNAMIC_PROVIDER);
assert(fixture.artifacts && fixture.baseUrl && fixture.secondaryBaseUrl && fixture.privateToken, '必须显式指定正式产物目录和双目标测试环境');
const run = TestRunContext.fromTestFile('tests/integration/gateway/test_mcp_dynamic_provider.py');
const result = { phase: 'initialize', initialization: {}, frames: [], snapshots: [], requests: [], pageErrors: [],
  boundary: '正式双 Workbench／独立 Gateway／真实同ID Node VSIX Provider；原 source/CAS、SDK stdio、跨目标拒绝和取消后迟到resolve；第二保存的真实成功响应被中断' };
const sourcePath = '/api/v1/config/sources/workspace_root_mutable_override';
const redact = value => String(value).replaceAll(fixture.privateToken, '<private>');
const save = () => writeFile(join(fixture.artifacts, 'dynamic-provider-browser.json'), redact(JSON.stringify(result, null, 2)));
async function phase(value) { result.phase = value; await save(); }
let page, failure, owner;
const apiFor = (targetPage, getOwner) => (path, method = 'GET', payload) => targetPage.evaluate(async ({ path, method, payload, owner }) => {
  const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
  const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token,
    ...(owner && path.startsWith('/api/v1/') ? { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
  const body = await response.json();
  if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, body }));
  return body;
}, { path, method, payload, owner: getOwner() });
function observeTarget(targetPage, record) {
  targetPage.on('pageerror', error => record.pageErrors.push(redact(error)));
  targetPage.on('request', request => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith(sourcePath) && !url.pathname.startsWith('/api/v1/mcp/')) return;
    const body = request.postData() ? request.postDataJSON() : undefined;
    assert(!JSON.stringify(body ?? {}).includes(fixture.privateToken), '公开配置请求不得包含 resolve 秘密');
    record.requests.push({ method: request.method(), path: url.pathname + url.search, body });
  });
  targetPage.on('websocket', socket => {
    if (new URL(socket.url()).pathname !== '/api/gateway/extension-hosts/mcp-definitions') return;
    for (const direction of ['framesent', 'framereceived']) socket.on(direction, ({ payload }) => {
      const frame = JSON.parse(String(payload)), privateValue = String(payload).includes(fixture.privateToken);
      if (frame.kind === 'snapshot') {
        assert(!privateValue && !('launch' in frame), 'Provider snapshot 只能包含 metadata');
        record.snapshots.push(frame);
      }
      if (direction === 'framereceived' && frame.client_id) record.binding = frame;
      record.frames.push({ direction, kind: frame.kind ?? 'binding', request_id: frame.request_id, privateValue });
    });
  });
}
async function saveReference(target, serverId, reference) {
  const document = (await target.api(sourcePath)).data, source = JSON.parse(document.jsonc || '{}');
  source.mcp ??= {};
  source.mcp.servers ??= {};
  source.mcp.servers[serverId] = { enabled: true, transport: 'stdio', provider_definition: reference };
  const intent = { gateway_id: target.owner.gateway_id, workspace_id: target.owner.workspace_id,
    source_key: 'workspace_root_mutable_override', jsonc: JSON.stringify(source, null, 2) + '\n',
    base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
    expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: randomUUID() };
  await target.api(sourcePath + '/preflight', 'POST', intent);
  return (await target.api(sourcePath, 'PUT', intent)).data;
}
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('dynamic-provider-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('dynamic-provider-context', () => context.close());
  page = await context.newPage();
  observeTarget(page, result);
  await page.goto(new URL('/api/gateway/health', fixture.baseUrl).href);
  const api = apiFor(page, () => owner);
  await api('/api/gateway/users/guest', 'POST', { tracking: { source: 'dynamic-provider' } });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  owner = listing.data.active_workspace;
  const workspace = await api('/api/v1/workspace');
  assert.equal(health.data.gateway_id, owner.gateway_id); assert.equal(workspace.data.workspace_id, owner.workspace_id);
  result.initialization = { health, listing, workspace };
  const label = listing.data.items.find(item => item.workspace_id === owner.workspace_id).name;
  const roots = await api('/api/v1/workspace/files/roots');
  const entry = new URL(fixture.baseUrl);
  entry.searchParams.set('folder', `boxteam-file://${owner.gateway_id}/${owner.workspace_id}/${encodeURIComponent(encodeURIComponent(roots.data.workspace_uri))}`);
  await page.goto(entry.href, { waitUntil: 'domcontentloaded' });
  const trust = page.getByRole('button', { name: /Yes, I trust the authors/ });
  if (await trust.isVisible()) await trust.click();
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands);
  const command = (name) => page.evaluate(name => globalThis.workbench.commands.executeCommand(name), name);
  await expect.poll(async () => {
    try { return await command('provider-probe.state'); }
    catch (error) { if (!String(error).includes("command 'provider-probe.state' not found")) throw error; return undefined; }
  }, { timeout: 30_000 }).toEqual({ provided: 1, resolved: [] });
  await expect.poll(() => result.snapshots.filter(item => item.definitions.length === 2).length, { timeout: 30_000 }).toBeGreaterThan(0);
  const originalSnapshot = result.snapshots.at(-1);
  assert.equal(originalSnapshot.definitions[0].label, originalSnapshot.definitions[1].label);
  assert.equal(originalSnapshot.definitions[0].version, originalSnapshot.definitions[1].version);
  assert.notEqual(originalSnapshot.definitions[0].definition_ref, originalSnapshot.definitions[1].definition_ref);
  const baseline = (await api(sourcePath)).data;
  const quick = page.locator('.quick-input-widget');
  const title = text => expect(quick.locator('.quick-input-title')).toHaveText(text);
  const pick = async text => {
    const row = quick.locator('.monaco-list-row').filter({ has: page.getByText(text, { exact: true }) });
    await expect(row).toBeVisible(); await row.click();
  };
  // 原 command Promise 在 Quick Input 结束才完成，不能在点击前等待它。
  const startAdd = async (serverId, index) => {
    await page.keyboard.press('F1'); await quick.locator('input').fill('>MCP: Add Server');
    await pick('MCP: Add Server...');
    await title('选择 MCP 配置工作区'); await pick(label);
    await title('添加 MCP Server'); await quick.locator('input').fill(serverId); await page.keyboard.press('Enter');
    await title('选择 MCP 配置来源'); await pick('从扩展提供的 MCP 定义添加');
    await title('选择原扩展的 MCP 定义');
    await quick.locator('.monaco-list-row').filter({ hasText: originalSnapshot.definitions[index].definition_ref }).click();
    await title(`选择 ${serverId} 的启用状态`); await pick('启用此 Server');
    await title('确认 MCP Provider 配置');
  };
  await phase('cancel-source-save');
  await startAdd('cancelled-provider', 0); await pick('取消');
  await expect(quick).not.toBeVisible();
  assert.equal((await api(sourcePath)).data.file_digest, baseline.file_digest);
  assert.equal(result.requests.filter(item => item.method === 'PUT').length, 0);
  result.cancelledWithoutWrite = true;
  await phase('save-first-original');
  await startAdd('dynamic-0', 0); await pick('保存此 Provider 来源引用');
  await expect(quick).not.toBeVisible();
  await expect(page.locator('.notifications-toasts')).toContainText('Provider 配置提交');
  await expect.poll(async () => (await api('/api/v1/mcp/servers')).data.find(item => item.server_id === 'dynamic-0')?.status).toBe('ready');
  const firstSource = (await api(sourcePath)).data;
  result.firstSource = firstSource;
  assert.equal(firstSource.effective.mcp.servers['dynamic-0'].enabled, true);
  await phase('save-second-original-response-lost');
  let lostKey;
  await page.route('**' + sourcePath, async route => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    const intent = route.request().postDataJSON();
    lostKey = intent.idempotency_key;
    const response = await route.fetch(); assert.equal(response.status(), 200);
    assert.equal((await response.json()).data.edit.state, 'active');
    await route.abort('failed');
  }, { times: 20 });
  await startAdd('dynamic-1', 1); await pick('保存此 Provider 来源引用');
  await expect(quick).not.toBeVisible();
  await expect(page.locator('.notifications-toasts')).toContainText('Provider 配置保存未确认');
  await page.unroute('**' + sourcePath);
  await expect.poll(async () => (await api('/api/v1/mcp/servers')).data.find(item => item.server_id === 'dynamic-1')?.status).toBe('ready');
  assert(lostKey && result.requests.some(item => item.method === 'GET' && item.path.includes(encodeURIComponent(lostKey))), '丢失响应后须用原幂等键回读');
  assert.equal(result.requests.filter(item => item.method === 'PUT').length, 2, '不能重放未知结果的保存');
  result.resolved = await command('provider-probe.state');
  assert.deepEqual(result.resolved.resolved, [0, 1]);
  await phase('second-gateway-same-vsix-original-definition');
  const secondary = result.secondary = { initialization: {}, frames: [], snapshots: [], requests: [], pageErrors: [] };
  const secondContext = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('dynamic-provider-secondary-context', () => secondContext.close());
  const secondPage = await secondContext.newPage();
  observeTarget(secondPage, secondary);
  let secondOwner;
  const secondApi = apiFor(secondPage, () => secondOwner);
  await secondPage.goto(new URL('/api/gateway/health', fixture.secondaryBaseUrl).href);
  await secondApi('/api/gateway/users/guest', 'POST', { tracking: { source: 'dynamic-provider-B' } });
  const secondHealth = await secondApi('/api/gateway/health'), secondListing = await secondApi('/api/gateway/workspaces');
  secondOwner = secondListing.data.active_workspace;
  const secondWorkspace = await secondApi('/api/v1/workspace');
  assert.equal(secondHealth.data.gateway_id, secondOwner.gateway_id);
  assert.equal(secondWorkspace.data.workspace_id, secondOwner.workspace_id);
  assert.notEqual(secondOwner.gateway_id, owner.gateway_id);
  assert.notEqual(secondOwner.workspace_id, owner.workspace_id);
  secondary.initialization = { health: secondHealth, listing: secondListing, workspace: secondWorkspace };
  const secondRoots = await secondApi('/api/v1/workspace/files/roots'), secondEntry = new URL(fixture.secondaryBaseUrl);
  secondEntry.searchParams.set('folder', `boxteam-file://${secondOwner.gateway_id}/${secondOwner.workspace_id}/${encodeURIComponent(encodeURIComponent(secondRoots.data.workspace_uri))}`);
  await secondPage.goto(secondEntry.href, { waitUntil: 'domcontentloaded' });
  const secondTrust = secondPage.getByRole('button', { name: /Yes, I trust the authors/ });
  if (await secondTrust.isVisible()) await secondTrust.click();
  await expect(secondPage.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await secondPage.waitForFunction(() => !!globalThis.workbench?.commands);
  const secondCommand = name => secondPage.evaluate(name => globalThis.workbench.commands.executeCommand(name), name);
  await expect.poll(async () => {
    try { return await secondCommand('provider-probe.state'); }
    catch (error) { if (!String(error).includes("command 'provider-probe.state' not found")) throw error; return undefined; }
  }, { timeout: 30_000 }).toEqual({ provided: 1, resolved: [] });
  await expect.poll(() => secondary.snapshots.filter(item => item.definitions.length === 2).length, { timeout: 30_000 }).toBeGreaterThan(0);
  const secondSnapshot = secondary.snapshots.at(-1), secondBinding = secondary.binding;
  assert.equal(secondSnapshot.extension_id, originalSnapshot.extension_id);
  assert.equal(secondSnapshot.extension_key, originalSnapshot.extension_key);
  assert.notEqual(secondBinding.backend_id, result.binding.backend_id);
  assert.notEqual(secondBinding.generation, result.binding.generation);
  const secondReference = { gateway_id: secondBinding.gateway_id, backend_id: secondBinding.backend_id,
    client_id: secondBinding.client_id, view_id: secondBinding.view_id, host_generation: secondBinding.generation,
    host_kind: secondBinding.host_kind, extension_id: secondSnapshot.extension_id, extension_key: secondSnapshot.extension_key,
    collection_id: secondSnapshot.collection_id, provider_ref: secondSnapshot.provider_ref,
    definition_ref: secondSnapshot.definitions[0].definition_ref };
  const secondTarget = { api: secondApi, owner: secondOwner };
  secondary.firstSource = await saveReference(secondTarget, 'dynamic-0', secondReference);
  await expect.poll(async () => (await secondApi('/api/v1/mcp/servers')).data.find(item => item.server_id === 'dynamic-0')?.status).toBe('ready');
  const secondBefore = (await secondApi('/api/v1/mcp/servers')).data.find(item => item.server_id === 'dynamic-0');
  assert.deepEqual(await secondCommand('provider-probe.state'), { provided: 1, resolved: [0] });
  await phase('foreign-gateway-definition-is-unavailable');
  secondary.foreignSource = await saveReference(secondTarget, 'foreign-provider', firstSource.effective.mcp.servers['dynamic-0'].provider_definition);
  await expect.poll(async () => (await secondApi('/api/v1/mcp/servers')).data.find(item => item.server_id === 'foreign-provider')?.status).toBe('unavailable');
  const foreign = (await secondApi('/api/v1/mcp/servers')).data.find(item => item.server_id === 'foreign-provider');
  assert(foreign.unavailable_reason && !foreign.connection_ref && foreign.tools.length === 0);
  assert.deepEqual(foreign.provider_definition, firstSource.effective.mcp.servers['dynamic-0'].provider_definition);
  assert.deepEqual(await secondCommand('provider-probe.state'), { provided: 1, resolved: [0] });
  secondary.foreignRejected = foreign;
  await phase('cancel-held-resolve-and-release-late-launch');
  await command('provider-probe.hold');
  const privateFramesBeforeLate = result.frames.filter(item => item.kind === 'resolved' && item.privateValue).length;
  const lateSave = saveReference({ api, owner }, 'late-provider', firstSource.effective.mcp.servers['dynamic-0'].provider_definition)
    .then(document => ({ document }), error => ({ error: redact(error?.stack ?? error) }));
  await expect.poll(async () => (await command('provider-probe.state')).resolved).toEqual([0, 1, 0]);
  const heldRequest = result.frames.findLast(item => item.direction === 'framereceived' && item.kind === 'resolve').request_id;
  await phase('change-and-dispose-preserve-running-connections');
  await command('provider-probe.change');
  await expect.poll(() => result.snapshots.length).toBeGreaterThan(1);
  await expect.poll(async () => (await command('provider-probe.hold-state')).cancelled).toBe(1);
  await expect.poll(() => result.frames.some(item => item.direction === 'framereceived' && item.kind === 'cancel' && item.request_id === heldRequest)).toBe(true);
  await command('provider-probe.release');
  await expect.poll(async () => (await command('provider-probe.hold-state')).returned).toBe(1);
  const lateSaved = await lateSave;
  assert(!lateSaved.error, lateSaved.error);
  await expect.poll(async () => (await api('/api/v1/mcp/servers')).data.find(item => item.server_id === 'late-provider')?.status).toBe('unavailable');
  const lateStatus = (await api('/api/v1/mcp/servers')).data.find(item => item.server_id === 'late-provider');
  assert(lateStatus.unavailable_reason && !lateStatus.connection_ref && lateStatus.tools.length === 0);
  assert.equal(result.frames.filter(item => item.kind === 'resolved' && item.privateValue).length, privateFramesBeforeLate);
  result.lateResolve = { request_id: heldRequest, state: await command('provider-probe.hold-state'), saved: lateSaved.document, status: lateStatus };
  await command('provider-probe.dispose');
  const statuses = (await api('/api/v1/mcp/servers')).data;
  assert(statuses.filter(item => item.server_id.startsWith('dynamic-')).every(item => item.status === 'ready' && item.connection_ref));
  result.runningAfterDispose = statuses;
  const secondAfter = (await secondApi('/api/v1/mcp/servers')).data.find(item => item.server_id === 'dynamic-0');
  assert.equal(secondAfter.status, 'ready');
  assert.equal(secondAfter.connection_ref, secondBefore.connection_ref);
  assert.deepEqual(await secondCommand('provider-probe.state'), { provided: 1, resolved: [0] });
  assert.equal(secondary.frames.filter(item => item.kind === 'resolved' && item.privateValue).length, 1);
  assert(secondary.frames.filter(item => item.kind !== 'resolved').every(item => !item.privateValue));
  secondary.runningAfterForeignAndPrimaryDispose = secondAfter;
  assert.deepEqual(secondary.pageErrors, []);
  assert.equal(result.frames.filter(item => item.kind === 'resolved' && item.privateValue).length, 2);
  assert(result.frames.filter(item => item.kind !== 'resolved').every(item => !item.privateValue));
  assert.deepEqual(result.pageErrors, []);
  await page.screenshot({ path: join(fixture.artifacts, 'dynamic-provider-running.png'), fullPage: true });
  await phase('complete');
} catch (error) {
  failure = error;
  result.error = redact(error?.stack ?? error);
  if (page) await page.screenshot({ path: join(fixture.artifacts, 'dynamic-provider-failure.png'), fullPage: true }).catch(() => {});
} finally {
  await save();
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], 'Provider 验证及清理失败') : error; }
}
if (failure) throw failure;
