import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_ARTIFACT_MANAGEMENT);
assert(fixture.artifacts && fixture.artifact.binding, '必须指定原制品与产物目录');
const run = TestRunContext.fromTestFile('tests/integration/gateway/test_mcp_artifact_management.py');
const result = { phase: 'initialize', pageErrors: [], deletes: [], initialization: [], responses: [],
  boundary: '真实Gateway／Backend／uv发布与原Workbench命令面板；引用仅由原source/CAS建立，无HTTP或业务状态替身' };
let page, failure;
const pending = [];
const save = () => writeFile(join(fixture.artifacts, 'artifact-management.json'), JSON.stringify(result, null, 2));
async function phase(value) { result.phase = value; await save(); }
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('artifact-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('artifact-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('response', response => {
    if (!new URL(response.url()).pathname.includes('/mcp/installations')) return;
    pending.push((async () => {
      const body = await response.json();
      assert.equal(body.request_id, response.headers()['x-request-id']);
      const item = { method: response.request().method(), url: response.url(), status: response.status(), body,
        requestBody: response.request().method() === 'DELETE' ? response.request().postDataJSON() : undefined };
      result.responses.push(item);
      if (item.method === 'DELETE') result.deletes.push(item);
    })());
  });
  await page.goto(new URL('/api/gateway/health', fixture.baseUrl).href);
  let owner;
  const api = async (path, method = 'GET', payload) => page.evaluate(async ({ path, method, payload, owner }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token,
      ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) },
      ...(payload ? { body: JSON.stringify(payload) } : {}) });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, body }));
    return body.data;
  }, { path, method, payload, owner });
  await api('/api/gateway/users/guest', 'POST', { tracking: { source: 'artifact-management' } });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  owner = listing.active_workspace;
  const workspace = await api('/api/v1/workspace');
  assert.equal(owner.gateway_id, health.gateway_id); assert.equal(owner.workspace_id, workspace.workspace_id);
  result.initialization = { health, owner, workspace };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const quick = page.locator('.quick-input-widget');
  const label = listing.items.find(item => item.workspace_id === owner.workspace_id).name;
  const waitList = async (hasArtifact = true) => {
    // 引用页标题也含包名；必须等原列表重新打开，不能拿旧标题判断返回已完成。
    await expect(quick.locator('.quick-input-title')).toHaveText(`MCP 制品 · ${label}`);
    const installed = quick.locator('.monaco-list-row').filter({ has: page.getByText('artifact-server 1.0.0', { exact: true }) });
    if (hasArtifact) await expect(installed).toBeVisible();
    else { await expect(installed).toHaveCount(0); await expect(quick.locator('input')).toHaveAttribute('placeholder', /^暂无制品/); }
  };
  const pick = async label => { await quick.locator('.monaco-list-row').filter({ has: page.getByText(label, { exact: true }) }).click(); };
  const open = async () => {
    await page.keyboard.press('F1'); await quick.locator('input').fill('>MCP: 管理目标制品');
    await page.keyboard.press('Enter');
    await expect(quick).toContainText('选择 MCP 制品目标');
    await pick(label);
    await waitList();
  };
  const artifact = async action => { await pick('artifact-server 1.0.0'); await pick(action); };
  const sourcePath = '/api/v1/config/sources/workspace_root_mutable_override';
  const baseline = JSON.parse((await api(sourcePath)).jsonc);
  const reference = async enabled => {
    const document = await api(sourcePath), config = JSON.parse(document.jsonc);
    config.mcp = enabled ? { servers: { retained: { enabled: true, transport: 'stdio', managed_launch: fixture.artifact.binding } } } : baseline.mcp;
    const saved = await api(sourcePath, 'PUT', { gateway_id: owner.gateway_id, workspace_id: owner.workspace_id,
      source_key: document.source_key, jsonc: JSON.stringify(config), base_file_digest: document.file_digest,
      base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
      expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: crypto.randomUUID() });
    assert.equal(saved.edit.state, 'active');
    return saved;
  };
  await phase('cancelled-browsing');
  await open(); await page.keyboard.press('Escape'); await expect(quick).not.toBeVisible();
  assert.equal(result.deletes.length, 0); result.cancelledWithoutDelete = true;
  await phase('retained-references');
  await reference(true);
  await open(); await artifact('物理卸载此制品');
  await expect(quick).toContainText('保留引用'); await expect(quick).toContainText('retained');
  assert.equal(result.deletes.length, 0); result.referencesRetained = true;
  await page.keyboard.press('Escape'); await waitList();
  await page.keyboard.press('Escape'); await expect(quick).not.toBeVisible();
  await reference(false);
  await phase('cancelled-confirmation');
  await open(); await artifact('物理卸载此制品'); await expect(quick).toContainText('确认物理卸载');
  await page.keyboard.press('Escape'); await waitList();
  assert.equal(result.deletes.length, 0);
  await phase('reference-created-after-precheck');
  await artifact('物理卸载此制品'); await expect(quick).toContainText('确认物理卸载');
  await reference(true);
  await pick('确认物理卸载');
  await expect(quick).toContainText('保留引用'); await expect(quick).toContainText('retained');
  await Promise.all(pending);
  assert.equal(result.deletes.length, 1); assert.equal(result.deletes[0].status, 409);
  assert.deepEqual(result.deletes[0].body.data.binding, fixture.artifact.binding);
  result.raceRejected = true;
  await page.keyboard.press('Escape');
  const rejected = page.getByRole('dialog', { name: /MCP: 管理目标制品/ });
  await expect(rejected).toContainText('请求失败 409');
  await expect(rejected).toContainText('MCP 制品仍有实际引用');
  await rejected.getByRole('button', { name: 'OK', exact: true }).click();
  await reference(false);
  await phase('explicit-uninstall');
  await open(); await artifact('物理卸载此制品'); await pick('确认物理卸载');
  await expect(page.locator('.notifications-toasts')).toContainText('卸载已完成');
  await waitList(false);
  await Promise.all(pending);
  assert.equal(result.deletes.length, 2); assert.equal(result.deletes[1].status, 200);
  const operationId = result.deletes[1].requestBody.operation_id;
  await pick('查询已有安装／卸载操作');
  await expect(quick).toContainText('查询原 MCP 操作');
  await quick.locator('input').fill(operationId);
  const queried = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/mcp/installation-operations/${operationId}`);
  await page.keyboard.press('Enter');
  const receipt = await queried, operation = await receipt.json();
  assert.equal(receipt.status(), 200); assert.equal(operation.request_id, receipt.headers()['x-request-id']);
  assert.equal(operation.data.operation_id, operationId); assert.equal(operation.data.status, 'succeeded');
  assert.equal(operation.data.artifact.state, 'removed'); assert.deepEqual(operation.data.artifact.binding, fixture.artifact.binding);
  result.queriedOperation = operation;
  await waitList(false);
  await page.keyboard.press('Escape'); await expect(quick).not.toBeVisible();
  await Promise.all(pending);
  assert.equal(result.deletes.length, 2); assert.equal(result.deletes[1].status, 200);
  assert.notEqual(result.deletes[0].requestBody.operation_id, result.deletes[1].requestBody.operation_id);
  for (const item of result.deletes) {
    assert.deepEqual(item.requestBody.target, fixture.target);
    assert.deepEqual(item.requestBody.binding, fixture.artifact.binding);
  }
  assert.deepEqual(await api('/api/v1/mcp/installations'), []);
  assert.deepEqual(await api('/api/v1/mcp/servers'), []); assert.deepEqual(result.pageErrors, []);
  await page.screenshot({ path: join(fixture.artifacts, 'artifact-management-complete.png'), fullPage: true });
  await phase('complete');
} catch (error) { failure = error; result.error = String(error?.stack ?? error); }
finally {
  if (failure && page) {
    try { await page.screenshot({ path: join(fixture.artifacts, 'artifact-management-failure.png'), fullPage: true }); }
    catch (error) { failure = new AggregateError([failure, error], '管理场景与截图均失败'); }
  }
  try { await Promise.all(pending); await save(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '场景与证据保存均失败') : error; }
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '场景与回收均失败') : error; }
}
if (failure) throw failure;
