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
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  run.addCleanup('Ports BrowserContext', () => context.close());
  page = await context.newPage(); page.setDefaultTimeout(20_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const api = (path, owner) => page.evaluate(async ({ path, owner }) => {
    const auth = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(path, { headers: { 'X-Local-Token': auth.token,
      ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } : {}) } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, body }));
    }
    return body.data;
  }, { path, owner });
  await api('/api/gateway/health');
  const registered = await api('/api/gateway/workspaces?check_health=false');
  assert(registered.items.some(item => item.gateway_id === fixture.gatewayId && item.workspace_id === fixture.workspaceId
    && item.connection_kind === 'remote_gateway'));
  assert.equal((await api('/api/v1/workspace', fixture)).workspace_id, fixture.workspaceId);
  const row = page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`);
  await row.locator('.workspace-label').click();
  await expect(row).toHaveAttribute('aria-current', 'location');
  await page.evaluate(() => globalThis.workbench.commands.executeCommand('boxteam.ports.focus'));
  const panel = page.locator(`.native-workspace-panel[data-panel-kind="ports"][data-workspace-id="${fixture.workspaceId}"]`);
  await expect(panel).toBeVisible();
  const endpoint = `/api/gateway/workspaces/${fixture.workspaceId}/port-forwards`;
  const receipt = async (method, action, trigger) => {
    const [response] = await Promise.all([
      page.waitForResponse(response => response.request().method() === method
        && new URL(response.url()).pathname === endpoint + action, { timeout: 30_000 }),
      trigger(),
    ]);
    const body = await response.json(), headers = response.request().headers();
    assert.equal(response.status(), 200); assert.equal(body.request_id, response.headers()['x-request-id']);
    assert.equal(headers['x-boxteam-target-gateway-id'], fixture.gatewayId);
    assert.equal(headers['x-boxteam-workspace-id'], fixture.workspaceId);
    result.receipts.push({ method, path: endpoint + action, requestId: body.request_id, data: body.data });
    return body.data;
  };
  result.phase = 'create';
  await panel.getByRole('button', { name: '新增端口', exact: true }).click();
  await panel.getByLabel('远端端口', { exact: true }).fill(String(fixture.remotePort));
  await panel.getByLabel('本地端口（可选）', { exact: true }).fill(String(fixture.localPort));
  await panel.getByLabel('名称（可选）', { exact: true }).fill('管理入口 SSH');
  const refreshed = await receipt('GET', '', () => panel.getByRole('button', { name: '刷新工作区状态', exact: true }).click());
  assert.equal(refreshed.items.length, 0);
  await expect(panel.getByLabel('远端端口', { exact: true })).toHaveValue(String(fixture.remotePort));
  await expect(panel.getByLabel('本地端口（可选）', { exact: true })).toHaveValue(String(fixture.localPort));
  await expect(panel.getByLabel('名称（可选）', { exact: true })).toHaveValue('管理入口 SSH');
  result.draftPreservedAfterOwnerRefresh = true;
  const created = await receipt('POST', '', () => panel.getByRole('button', { name: '创建转发', exact: true }).click());
  const forward = created.items.find(item => item.remote_port === fixture.remotePort);
  assert(forward); assert.equal(forward.status, 'active');
  assert.equal(forward.gateway_id, fixture.gatewayId); assert.equal(forward.workspace_id, fixture.workspaceId);
  assert.equal(forward.local_port, fixture.localPort);
  result.forward = forward;
  const item = panel.getByRole('row').filter({ hasText: '管理入口 SSH' });
  await expect(item).toHaveCount(1);
  result.phase = 'open';
  const popupOpened = context.waitForEvent('page');
  await item.getByRole('link').click();
  const popup = await popupOpened;
  await expect(popup.getByRole('heading')).toHaveText(fixture.marker);
  assert.equal(popup.url(), forward.local_url + '/');
  result.openedThroughSsh = true;
  await popup.close();
  result.phase = 'stop';
  await item.click({ button: 'right' });
  const menu = item.getByRole('menu');
  await expect(menu).toBeVisible();
  const stopped = await receipt('DELETE', '/' + forward.forward_id, async () => {
    await menu.getByRole('menuitem', { name: '停止转发', exact: true }).click();
    const confirm = page.getByRole('dialog').filter({ hasText: '停止端口转发' });
    await expect(confirm).toBeVisible();
    await confirm.getByRole('button', { name: '停止转发', exact: true }).click();
  });
  assert.equal(stopped.items.length, 0);
  await expect(item).toHaveCount(0);
  const remaining = await api(endpoint, fixture);
  assert.equal(remaining.items.length, 0);
  const unavailable = await page.request.get(forward.local_url, { timeout: 3000, maxRetries: 0 }).then(() => false,
    cause => { assert.match(String(cause), /ECONNREFUSED/); return true; });
  assert(unavailable);
  const original = await page.request.get(`http://127.0.0.1:${fixture.remotePort}`);
  assert(original.ok()); assert((await original.text()).includes(fixture.marker));
  result.stoppedForwardOnly = true;
  assert.deepEqual(result.pageErrors, []);
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
