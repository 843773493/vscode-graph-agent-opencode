import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_PORT_MANAGEMENT_FIXTURE);
const artifacts = process.env.BOXTEAM_PORT_MANAGEMENT_ARTIFACTS;
assert(artifacts, '必须显式指定 Ports 验收产物目录');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/test_gateway_connection_management.py');
const result = { phase: 'initialization', pageErrors: [], receipts: [] };
const failures = [];
let page;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Ports Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  run.addCleanup('Ports BrowserContext', () => context.close());
  page = await context.newPage(); page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const api = path => page.evaluate(async path => {
    const auth = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(path, { headers: { 'X-Local-Token': auth.token } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return body.data;
  }, path);
  const owner = (await api('/api/gateway/health')).gateway_id;
  await api('/api/gateway/workspaces?check_health=false');
  // 从实际工作台菜单进入，不先激活远端工作区或会话。
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('boxteam.ports.focus'));
  const panel = page.locator('.host-ports-view');
  await expect(panel).toBeVisible();
  await expect(page.getByRole('button', { name: /^选择面板目标/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^选择面板工作区/ })).toHaveCount(0);
  const endpoint = '/api/gateway/port-forwards';
  const hosts = await api(endpoint + '/hosts'); result.hosts = hosts;
  assert(hosts.items.some(host => host.host_id === 'rgw_c3_b'), '装具的 SSH Host 必须由真实目录公布');
  const receipt = async (method, suffix, trigger) => {
    const [response] = await Promise.all([
      page.waitForResponse(response => response.request().method() === method && new URL(response.url()).pathname === endpoint + suffix), trigger(),
    ]);
    const body = await response.json();
    assert.equal(response.status(), 200); assert.equal(body.request_id, response.headers()['x-request-id']);
    assert(!('x-boxteam-workspace-id' in response.request().headers()));
    result.receipts.push({ method, path: endpoint + suffix, requestId: body.request_id, data: body.data });
    return body.data;
  };
  const cell = (id, field) => panel.locator(`.port-grid-cell[data-port-id="${id}"][data-field="${field}"]`);
  const row = id => cell(id, 'label').locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " monaco-list-row ")]');
  const enter = async (id, field, value) => {
    await cell(id, field).dblclick();
    const input = cell(id, field).locator('input');
    await input.fill(value); await input.press('Enter');
  };
  result.phase = 'create';
  await page.getByRole('button', { name: '添加端口转发', exact: true }).first().click();
  // 新建行的首格选择器已获焦点，先切到另一格，选择远端服务 Host。
  await page.keyboard.press('Escape');
  const addressCell = cell('new-forward', 'listenerAddress');
  const beforeEdit = await addressCell.locator('.port-grid-label').evaluate(node => {
    const range = document.createRange(); range.selectNodeContents(node);
    const rect = range.getBoundingClientRect(), style = getComputedStyle(node);
    return { left: rect.left, centerY: (rect.top + rect.bottom) / 2,
      family: style.fontFamily, size: style.fontSize, weight: style.fontWeight };
  });
  await addressCell.dblclick();
  const editingInput = addressCell.locator('input');
  await expect(editingInput).toBeFocused();
  assert(await editingInput.evaluate(node => node.selectionStart === 0 && node.selectionEnd === node.value.length), '原生行内编辑应选中完整值');
  const duringEdit = await editingInput.evaluate(node => {
    const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
    const parent = node.closest('.port-grid-label').getBoundingClientRect();
    return { left: rect.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
      centerY: (rect.top + rect.bottom + parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)) / 2,
      top: rect.top, bottom: rect.bottom, parentTop: parent.top, parentBottom: parent.bottom,
      family: style.fontFamily, size: style.fontSize, weight: style.fontWeight };
  });
  assert.deepEqual([duringEdit.family, duringEdit.size, duringEdit.weight], [beforeEdit.family, beforeEdit.size, beforeEdit.weight]);
  assert(Math.abs(duringEdit.left - beforeEdit.left) <= 0.5, JSON.stringify({ beforeEdit, duringEdit }));
  assert(Math.abs(duringEdit.centerY - beforeEdit.centerY) <= 1, JSON.stringify({ beforeEdit, duringEdit }));
  assert(duringEdit.top >= duringEdit.parentTop && duringEdit.bottom <= duringEdit.parentBottom, '输入行盒不能溢出单元格');
  result.editingAlignment = { beforeEdit, duringEdit };
  await page.screenshot({ path: artifacts + '/ports-editing.png' });
  await editingInput.fill('取消这次修改');
  await editingInput.press('Escape');
  await expect(addressCell).toHaveText('127.0.0.1');
  await addressCell.dblclick();
  await addressCell.locator('input').press('Tab');
  await expect(cell('new-forward', 'listenerPort').locator('input')).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(addressCell.locator('input')).toBeFocused();
  await addressCell.locator('input').press('Enter');
  await cell('new-forward', 'label').dblclick();
  await cell('new-forward', 'label').locator('input').fill('失焦保存');
  await panel.locator('.monaco-table-th').first().click();
  await expect(cell('new-forward', 'label')).toHaveText('失焦保存');
  await expect(panel.locator('input')).toHaveCount(0);
  result.nativeInlineEditing = { selection: true, cancel: true, tab: true, reverseTab: true, blurAccept: true };
  await enter('new-forward', 'listenerPort', String(fixture.localPort));
  await enter('new-forward', 'destinationPort', String(fixture.remotePort));
  await enter('new-forward', 'label', 'Host 间 SSH 转发');
  await cell('new-forward', 'destinationHost').dblclick();
  const serviceHost = cell('new-forward', 'destinationHost').locator('select');
  const remote = serviceHost.locator('option').filter({ hasText: 'C3 b' });
  await expect(remote).toHaveCount(1);
  await serviceHost.selectOption(await remote.getAttribute('value'));
  await receipt('GET', '', () => page.getByRole('button', { name: '刷新端口转发', exact: true }).click());
  await expect(cell('new-forward', 'listenerPort')).toHaveText(String(fixture.localPort));
  result.draftPreservedAfterOwnerRefresh = true;
  const created = await receipt('POST', '', () => cell('new-forward', 'state').getByRole('button', { name: '启动转发', exact: true }).click());
  const forward = created.items.find(item => item.listener.port === fixture.localPort);
  assert(forward); assert.equal(forward.status, 'active'); assert.equal(forward.gateway_id, owner);
  assert.equal(forward.destination.host_id, 'rgw_c3_b'); assert.equal(forward.destination.port, fixture.remotePort); assert(!('workspace_id' in forward));
  result.forward = forward;
  result.editorsAfterSave = await panel.locator('input,select').evaluateAll(nodes => nodes.map(node => ({ tag: node.tagName, value: node.value, parent: node.parentElement?.outerHTML })));
  await expect(panel.locator('input,select')).toHaveCount(0);
  await expect(panel.locator('.port-grid-label.select-container')).toHaveCount(0);
  result.phase = 'transmission';
  const forwarded = `http://127.0.0.1:${fixture.localPort}`;
  assert((await (await page.request.get(forwarded)).text()).includes(fixture.marker));
  result.openedThroughSsh = true;
  const workspace = page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`);
  await workspace.locator('.workspace-label').click();
  await expect(cell(forward.forward_id, 'label')).toContainText('Host 间 SSH 转发');
  await page.screenshot({ path: artifacts + '/ports-normal.png' });
  result.phase = 'refresh-layout';
  const layout = () => panel.locator('.monaco-table').evaluate(node => {
    const box = node.getBoundingClientRect();
    return { top: box.top, left: box.left, width: box.width, height: box.height };
  });
  const stableLayout = await layout();
  let releaseRead, enterRead;
  const readGate = new Promise(resolve => { releaseRead = resolve; });
  const readEntered = new Promise(resolve => { enterRead = resolve; });
  const delayedRead = async route => {
    if (route.request().method() === 'GET') { enterRead(); await readGate; }
    await route.continue();
  };
  await page.route('**/api/gateway/port-forwards', delayedRead);
  try {
    await page.getByRole('button', { name: '刷新端口转发', exact: true }).click();
    await readEntered;
    await expect(page.getByRole('button', { name: '刷新端口转发', exact: true })).toBeDisabled();
    assert.deepEqual(await layout(), stableLayout);
    await expect(cell(forward.forward_id, 'label')).toContainText('Host 间 SSH 转发');
    await page.screenshot({ path: artifacts + '/ports-refresh-pending.png' });
    await receipt('GET', '', () => releaseRead());
  } finally { releaseRead(); await page.unroute('**/api/gateway/port-forwards', delayedRead); }
  const failedRead = route => route.request().method() === 'GET' ? route.abort('connectionfailed') : route.continue();
  await page.route('**/api/gateway/port-forwards', failedRead);
  try {
    await expect(page.getByRole('button', { name: '刷新端口转发', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '刷新端口转发', exact: true }).click();
    await expect(panel.locator('.host-ports-status')).toBeVisible();
    assert.deepEqual(await layout(), stableLayout);
    await expect(cell(forward.forward_id, 'label')).toContainText('Host 间 SSH 转发');
    await page.screenshot({ path: artifacts + '/ports-refresh-failed.png' });
  } finally { await page.unroute('**/api/gateway/port-forwards', failedRead); }
  await receipt('GET', '', () => page.getByRole('button', { name: '刷新端口转发', exact: true }).click());
  await expect(panel.locator('.host-ports-status')).toBeHidden();
  assert.deepEqual(await layout(), stableLayout);
  result.refreshLayoutStable = true;
  result.phase = 'stop-restart';
  const stopped = await receipt('POST', `/${forward.forward_id}/stop`, () => cell(forward.forward_id, 'state').getByRole('button', { name: '停止转发', exact: true }).click());
  assert.equal(stopped.items.length, 1); assert.equal(stopped.items[0].status, 'stopped');
  const unavailable = await page.request.get(forwarded, { timeout: 2000, maxRetries: 0 }).then(() => false, () => true);
  assert(unavailable, '停止后监听端口应关闭');
  const restarted = await receipt('POST', `/${forward.forward_id}/start`, () => cell(forward.forward_id, 'state').getByRole('button', { name: '启动转发', exact: true }).click());
  assert.equal(restarted.items[0].forward_id, forward.forward_id);
  assert((await (await page.request.get(forwarded)).text()).includes(fixture.marker));
  result.stoppedForwardOnly = true;
  result.phase = 'edit';
  await row(forward.forward_id).click(); await page.keyboard.press('F2');
  const name = cell(forward.forward_id, 'label').locator('input');
  await name.fill('已编辑的转发');
  const updated = await receipt('PUT', `/${forward.forward_id}`, () => name.press('Enter'));
  assert.equal(updated.items[0].forward_id, forward.forward_id); assert.equal(updated.items[0].status, 'active');
  await expect(cell(forward.forward_id, 'label')).toContainText('已编辑的转发');
  // 原生右键子菜单显示三种协议，不能退回独立编辑表单。
  await cell(forward.forward_id, 'label').click({ button: 'right' });
  await page.getByRole('menuitem', { name: '服务类型', exact: false }).hover();
  const tcp = page.getByRole('menuitemcheckbox', { name: /TCP$/ });
  await expect(tcp).toBeVisible(); await expect(tcp).toHaveAttribute('aria-checked', 'true');
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape');
  result.phase = 'failure-recovery';
  // 与服务端相同入口形成必然冲突；错误必须单独占列，不增高行或污染名称格。
  const invalid = await receipt('PUT', `/${forward.forward_id}`, () => enter(forward.forward_id, 'listenerPort', String(fixture.remotePort)));
  assert.equal(invalid.items[0].status, 'error');
  const error = cell(forward.forward_id, 'error'); await expect(error).not.toHaveText('');
  const height = await row(forward.forward_id).evaluate(node => node.getBoundingClientRect().height);
  assert.equal(height, 26);
  await expect(cell(forward.forward_id, 'label')).toContainText('已编辑的转发');
  await page.screenshot({ path: artifacts + '/ports-error.png' });
  const repaired = await receipt('PUT', `/${forward.forward_id}`, () => enter(forward.forward_id, 'listenerPort', String(fixture.localPort)));
  assert.equal(repaired.items[0].status, 'active');
  await expect(cell(forward.forward_id, 'state').getByRole('button', { name: '停止转发', exact: true })).toBeEnabled();
  await page.setViewportSize({ width: 660, height: 560 });
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.closeSidebar'));
  await expect(cell(forward.forward_id, 'label')).toBeVisible();
  await page.screenshot({ path: artifacts + '/ports-narrow.png' });
  // 窄宽度通过原生横向滚动访问后面的列，而不是隐藏参数。
  await error.scrollIntoViewIfNeeded(); await expect(error).toBeInViewport();
  await page.screenshot({ path: artifacts + '/ports-narrow-errors.png' });
  await page.setViewportSize({ width: 660, height: 360 });
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('workbench.action.toggleLightDarkThemes'));
  await expect(page.locator('.monaco-workbench.vs-dark')).toBeVisible();
  await page.screenshot({ path: artifacts + '/ports-low-dark.png' });
  result.phase = 'delete';
  await cell(forward.forward_id, 'label').click({ button: 'right' });
  const remove = page.getByRole('menuitem', { name: '删除转发规则', exact: true });
  result.deleteMenuDisabled = await remove.getAttribute('aria-disabled');
  await expect(remove).toBeEnabled();
  const deleted = await receipt('DELETE', `/${forward.forward_id}`, () => remove.click());
  assert.equal(deleted.items.length, 0);
  assert.equal((await api(endpoint)).items.length, 0);
  result.phase = 'completed';
} catch (cause) {
  failures.push(cause); result.failure = String(cause?.stack ?? cause);
  if (page) {
    try { await page.screenshot({ path: artifacts + '/failure.png', fullPage: true }); }
    catch (diagnosticError) { failures.push(diagnosticError); }
  }
} finally {
  try { await writeFile(artifacts + '/result.json', JSON.stringify(result, null, 2) + '\n'); }
  catch (diagnosticError) { failures.push(diagnosticError); }
  try { await run.close(); }
  catch (cleanupError) { failures.push(cleanupError); }
}
if (failures.length) throw new AggregateError(failures, 'Ports 产品验收失败');
