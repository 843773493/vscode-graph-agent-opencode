import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_TABS_PRODUCT);
const output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
assert(fixture?.base_url && fixture.targets && fixture.workspace_id && output, '必须显式提供真实目标与产物目录');
const run = await TestRunContext.fromTestFile('tests/integration/clients/web/test_empty_target_web.py').prepare();
const result = { phase: 'initialize', pageErrors: [], consoleErrors: [], receipts: [], bindings: [], resolves: [], snapshots: [],
  boundary: '正式 Workbench、双真实 Gateway/Backend、原安装器及两个真实目标 Node；公开 API 创建文件、vscode.changes 创建 MultiDiff、window.tabGroups 事件/枚举/关闭。真实点击切焦点，公开 moveActiveEditor 驱动原组移动；未开放的 tabGroups.move/$moveTab RPC 仍只由定向测试覆盖。vscode.changes 不提供 goToFileUri，该字段仅由实际 sourceTransform 定向测试覆盖。SSH/隧道是具名 loopback 装具，不代替真实 SSH、Windows 或 Electron。' };
const save = () => writeFile(output, JSON.stringify(result, null, 2) + '\n');
const phase = async value => { result.phase = value; await save(); };
let page, failure;
const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
const node = (label, name, ...args) => command('boxteam.tabs.' + label + '.' + name, ...args);
const read = label => node(label, 'read');
const group = (snapshot, column) => {
  const value = snapshot.groups.find(value => value.column === column);
  assert(value, '公开 TabGroups 缺少列 ' + column);
  return value;
};
const labels = (snapshot, column) => group(snapshot, column).tabs.map(tab => tab.label);
const file = (label, name) => 'tabs-' + label + '-' + name + '.ts';
const ownA = 'Tabs Own A (2 files)', ownB = 'Tabs Own B (2 files)', oneSidedA = 'Tabs One Sided A (1 file)';
const mixedInputs = 'Tabs Mixed Inputs (2 files)', crossTargetPair = 'Tabs Cross Target Pair (1 file)';
const tab = (snapshot, name) => snapshot.groups.flatMap(group => group.tabs).find(tab => tab.label === name);
const actualTab = name => page.locator('.tabs-container .tab').filter({ hasText: name });
async function snapshot(label) {
  const [a, b] = await Promise.all([read('a'), read('b')]);
  const value = { label, a, b };
  result.snapshots.push(value);
  await save();
  return value;
}
async function api(path, method = 'GET', payload, target) {
  const receipt = await page.evaluate(async ({ path, method, payload, target, workspace }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method, headers: {
      'Content-Type': 'application/json', 'X-Local-Token': credential.data.token,
      ...(target ? { 'X-BoxTeam-Target-Gateway-Id': target.gateway_id, 'X-BoxTeam-Workspace-Id': workspace } : {}),
    }, ...(payload ? { body: JSON.stringify(payload) } : {}) });
    return { path, status: response.status, header: response.headers.get('X-Request-ID'), body: await response.json() };
  }, { path, method, payload, target, workspace: fixture.workspace_id });
  result.receipts.push(receipt);
  assert.equal(receipt.status, 200); assert(receipt.body.request_id); assert.equal(receipt.body.request_id, receipt.header);
  return receipt.body.data;
}
async function ready() {
  await expect(page.locator('.monaco-workbench')).toBeVisible({ timeout: 30_000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand, null, { timeout: 30_000 });
}
async function expectLabels(label, column, names) {
  await expect.poll(async () => labels(await read(label), column), { timeout: 15_000 }).toEqual(names);
}
function uri(label, name) {
  return { scheme: 'boxteam-file', authority: fixture.targets[label].gateway_id,
    path: '/' + fixture.workspace_id + '/' + encodeURIComponent(result.roots[label]) + '/' + file(label, name) };
}
async function rendererChanges(title, rows) {
  await command('vscode.changes', title, rows);
  await expect(actualTab(title)).toBeVisible({ timeout: 15_000 });
  await command('workbench.action.keepEditor');
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--renderer-process-limit=2'] });
  run.addCleanup('目标标签 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1100 } });
  run.addCleanup('目标标签 BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error.stack ?? error)));
  page.on('console', entry => { if (entry.type() === 'error') result.consoleErrors.push(entry.text()); });
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/gateway/extension-hosts/resolve') result.resolves.push(request.postDataJSON());
  });
  page.on('websocket', socket => {
    if (new URL(socket.url()).pathname !== '/api/gateway/extension-hosts/channel') return;
    socket.on('framereceived', ({ payload }) => {
      if (typeof payload !== 'string') return;
      const binding = JSON.parse(payload);
      if (!binding.initialization?.workspace) return;
      const { protocol_version, gateway_id, backend_id, view_id, generation, workspace_ids, request_id } = binding;
      result.bindings.push({ protocol_version, gateway_id, backend_id, view_id, generation, workspace_ids, request_id,
        workspace: binding.initialization.workspace });
    });
  });
  await page.goto(new URL('/api/gateway/health', fixture.base_url).href);
  await api('/api/gateway/users/guest', 'POST', { tracking: { source: 'target-tabs-product' } });
  await page.goto(fixture.base_url, { waitUntil: 'domcontentloaded' });
  await ready();
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  assert.equal(health.gateway_id, fixture.targets.a.gateway_id);
  assert.equal(workspaces.active_workspace.gateway_id, health.gateway_id);
  assert.equal(workspaces.active_workspace.workspace_id, fixture.workspace_id);
  await api('/api/v1/workspace');
  result.roots = {};
  for (const label of ['a', 'b']) {
    result.roots[label] = (await api('/api/v1/workspace/files/roots', 'GET', undefined, fixture.targets[label])).workspace_uri;
  }

  await phase('attach-two-real-target-node-views');
  const navigation = page.waitForURL(url => url.searchParams.has('workspace'), { waitUntil: 'domcontentloaded', timeout: 30_000 });
  const attaching = command('boxteam.extensions.attachWorkspaceViews');
  const picker = page.locator('.quick-input-widget:visible');
  await expect(picker).toBeVisible();
  for (const target of Object.values(fixture.targets)) {
    await picker.locator('.monaco-list-row').filter({ hasText: target.gateway_id }).locator('.monaco-checkbox').click();
  }
  await picker.locator('input').press('Enter');
  await attaching.catch(error => { if (!String(error).includes('Execution context was destroyed')) throw error; });
  await navigation; await ready();
  for (const label of ['a', 'b']) {
    await expect.poll(async () => {
      try { return (await read(label)).extensionId; }
      catch (error) { if (!String(error).includes("command 'boxteam.tabs." + label + ".read' not found")) throw error; }
    }, { timeout: 30_000 }).toBe('boxteam-probe.tabs-' + label);
    const prepared = await node(label, 'prepare');
    assert.deepEqual(prepared.folders, [fixture.native_roots[label]]); assert.match(prepared.node, /^24\./);
  }
  assert.equal(result.bindings.length, 2);
  for (const label of ['a', 'b']) {
    const binding = result.bindings.find(binding => binding.gateway_id === fixture.targets[label].gateway_id);
    assert.equal(binding.backend_id, fixture.targets[label].backend_id); assert.deepEqual(binding.workspace_ids, [fixture.workspace_id]);
    assert(binding.generation && binding.request_id);
  }
  await command('workbench.action.closeAllEditors');

  await phase('interleaved-real-editors-and-foreign-focus');
  await node('a', 'open', { name: 'first' });
  await node('b', 'open', { name: 'first' });
  await node('a', 'open', { name: 'second' });
  await expectLabels('a', 1, [file('a', 'first'), file('a', 'second')]);
  await expectLabels('b', 1, [file('b', 'first')]);
  const initial = await snapshot('interleaved');
  assert.notEqual(initial.a.pid, initial.b.pid);
  await actualTab(file('b', 'first')).click();
  await expect.poll(async () => group(await read('a'), 1).activeTab).toBeNull();
  await expect.poll(async () => group(await read('b'), 1).activeTab).toBe(file('b', 'first'));

  await phase('real-tab-and-group-movement');
  await actualTab(file('a', 'second')).click();
  await command('moveActiveEditor', { to: 'first', by: 'tab' });
  await expectLabels('a', 1, [file('a', 'second'), file('a', 'first')]);
  const reordered = await snapshot('same-group-move');
  for (const name of ['first', 'second']) assert.equal(tab(reordered.a, file('a', name)).identity, tab(initial.a, file('a', name)).identity);
  assert.equal(group(reordered.a, 1).identity, group(initial.a, 1).identity);
  assert.equal(tab(reordered.b, file('b', 'first')).identity, tab(initial.b, file('b', 'first')).identity);
  await command('moveActiveEditor', { to: 'right', by: 'group' });
  await expectLabels('a', 1, [file('a', 'first')]);
  await expectLabels('a', 2, [file('a', 'second')]);
  await expectLabels('b', 2, []);
  const moved = await snapshot('new-group-move');
  assert.equal(moved.a.groups.length, 2); assert.equal(moved.b.groups.length, 2);
  assert.equal(group(moved.a, 1).identity, group(initial.a, 1).identity);
  assert.equal(tab(moved.a, file('a', 'first')).identity, tab(initial.a, file('a', 'first')).identity);
  assert.equal(tab(moved.b, file('b', 'first')).identity, tab(initial.b, file('b', 'first')).identity);
  assert(group(moved.a, 2).active && group(moved.b, 2).active); assert.equal(group(moved.b, 2).activeTab, null);

  await phase('real-multidiff-complete-endpoints');
  await node('a', 'changes');
  await expectLabels('a', 2, [file('a', 'second'), ownA]);
  await command('workbench.action.keepEditor');
  await expectLabels('b', 2, []);
  const own = await snapshot('owned-multidiff');
  assert.deepEqual(tab(own.a, ownA).input, { kind: 'multiDiff', textDiffs: [1, 2].map(index => ({
    original: fixture.native_roots.a + '/' + file('a', 'old' + index), modified: fixture.native_roots.a + '/' + file('a', 'new' + index),
  })) });
  await rendererChanges('Tabs Mixed Inputs', [
    [uri('a', 'new1'), uri('a', 'old1'), uri('a', 'new1')], [uri('b', 'first'), null, uri('b', 'first')],
  ]);
  // TODO：公开 changes 不提供 goToFileUri；真实跳转端点入口接入后补产品验收，当前由定向测试证明字段隔离。
  await rendererChanges('Tabs Cross Target Pair', [[uri('a', 'new1'), uri('a', 'old1'), uri('b', 'first')]]);
  await rendererChanges('Tabs One Sided A', [[uri('a', 'new2'), null, uri('a', 'new2')]]);
  await expectLabels('a', 2, [file('a', 'second'), ownA, oneSidedA]);
  await expectLabels('b', 2, []);
  const mixed = await snapshot('mixed-and-one-sided-inputs');
  assert.deepEqual(tab(mixed.a, oneSidedA).input, { kind: 'multiDiff', textDiffs: [] });
  assert.equal(tab(mixed.a, ownA).identity, tab(own.a, ownA).identity);
  await node('b', 'changes');
  await expectLabels('b', 2, [ownB]);
  await command('workbench.action.keepEditor');
  await expect.poll(async () => group(await read('a'), 2).activeTab).toBeNull();
  const beforeClose = await snapshot('foreign-multidiff-active');

  await phase('public-close-retains-foreign-and-mixed-editors');
  assert.equal((await node('a', 'close', { name: ownA })).closed, true);
  await expect(actualTab(ownA)).toHaveCount(0);
  assert.equal((await node('a', 'close', { name: file('a', 'second'), wholeGroup: true })).closed, true);
  await expectLabels('a', 2, []); await expectLabels('b', 2, [ownB]);
  const afterClose = await snapshot('a-group-close');
  assert.equal(group(afterClose.a, 2).identity, group(beforeClose.a, 2).identity);
  assert.equal(tab(afterClose.b, ownB).identity, tab(beforeClose.b, ownB).identity);
  for (const title of [mixedInputs, crossTargetPair, ownB]) await expect(actualTab(title)).toBeVisible();
  await expect(actualTab(file('a', 'second'))).toHaveCount(0);
  await expect(actualTab(oneSidedA)).toHaveCount(0);
  assert.equal((await node('a', 'close', { name: file('a', 'first'), wholeGroup: true })).closed, true);
  await expectLabels('a', 1, []); await expectLabels('b', 1, [file('b', 'first')]);
  const final = await snapshot('all-a-tabs-closed');
  assert.equal(final.a.groups.length, 2); assert.equal(final.b.groups.length, 2);
  assert.equal(tab(final.b, file('b', 'first')).identity, tab(initial.b, file('b', 'first')).identity);
  assert.equal(tab(final.b, ownB).identity, tab(beforeClose.b, ownB).identity);
  assert(final.a.events.some(event => event.kind === 'tabs' && event.closed.some(tab => tab.label === ownA)));
  assert(final.a.events.every(event => event.kind !== 'tabs' || [...event.opened, ...event.closed, ...event.changed]
    .every(tab => ![mixedInputs, crossTargetPair, ownB, file('b', 'first')].includes(tab.label))));
  assert.deepEqual(result.pageErrors, []);
  result.mixedRetained = result.foreignRetained = result.identityPreserved = true;
  await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
  await phase('complete');
} catch (error) {
  failure = error; result.error = String(error.stack ?? error);
  if (page) {
    const diagnostics = await Promise.allSettled([
      page.screenshot({ path: output.replace(/\.json$/, '.failed.png'), fullPage: true }),
      page.locator('body').innerText().then(text => { result.visible = text; }),
    ]);
    const errors = diagnostics.filter(value => value.status === 'rejected').map(value => value.reason);
    if (errors.length) failure = new AggregateError([failure, ...errors], '标签产品验收及诊断失败');
  }
} finally {
  try { await save(); } catch (error) { failure = failure ? new AggregateError([failure, error], '标签验收及证据保存失败') : error; }
  try { await run.close(); result.cleanupCompleted = true; await save(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '标签验收及清理失败') : error; }
}
if (failure) throw failure;
