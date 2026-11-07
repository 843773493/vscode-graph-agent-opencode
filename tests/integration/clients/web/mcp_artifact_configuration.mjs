import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_ARTIFACT_CONFIGURATION);
assert(fixture.artifacts && fixture.registryUrl, '必须显式指定原目录与正式产物目录');
const run = TestRunContext.fromTestFile('tests/integration/gateway/test_mcp_artifact_configuration.py');
const result = { phase: 'initialize', pageErrors: [], initialization: [], requests: [], responses: [], drafts: [], saves: [],
  queriedOriginalCommit: false, boundary: '真实 Workbench／Gateway／Backend／uv；本地 registry 和 wheel 为受控输入；响应丢失变体仅丢弃真实 PUT 的回包，不伪造提交结果' };
let page, failure;
const pending = [];
const sourcePath = '/api/v1/config/sources/workspace_root_mutable_override';
const saveResult = () => writeFile(join(fixture.artifacts, 'artifact-configuration.json'), JSON.stringify(result, null, 2));
const phase = async value => { result.phase = value; await saveResult(); };
async function command(id, ...args) {
  await page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
}
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('configuration-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('configuration-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/v1/') && !path.startsWith('/api/gateway/mcp/')) return;
    const body = request.postData() ? request.postDataJSON() : undefined;
    const headers = Object.fromEntries(Object.entries(request.headers()).filter(([key]) =>
      ['x-boxteam-target-gateway-id', 'x-boxteam-workspace-id', 'x-request-id'].includes(key)));
    const item = { path, url: request.url(), method: request.method(), body, headers };
    if (body?.source_edit) {
      // 收据只记录正式脱敏 source；不可把测试输入之外的凭据写入证据。
      assert(!body.source_edit.jsonc.includes('artifact-fixture-key'));
    }
    result.requests.push(item);
    if (path === '/api/v1/mcp/candidate-configurations/draft') result.drafts.push(item);
    if (path === '/api/v1/mcp/candidate-configurations' && request.method() === 'PUT') result.saves.push(item);
  });
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (!/\/mcp\/(?:[^/]+\/[^/]+\/)?(installation|candidate)/.test(path) && !path.startsWith(sourcePath)) return;
    pending.push((async () => {
      const body = await response.json();
      assert.equal(body.request_id, response.headers()['x-request-id']);
      assert.equal(response.status(), 200, JSON.stringify(body));
      result.responses.push({ path, url: response.url(), method: response.request().method(), status: response.status(), body });
    })());
  });
  await page.goto(new URL('/api/gateway/health', fixture.baseUrl).href);
  let owner;
  const api = async path => page.evaluate(async ({ path, owner }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { headers: { 'X-Local-Token': credential.data.token,
      ...(owner ? { 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, body }));
    return body;
  }, { path, owner });
  await page.evaluate(async () => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch('/api/gateway/users/guest', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.data.token },
      body: JSON.stringify({ tracking: { source: 'artifact-configuration' } }) });
    if (!response.ok) throw new Error(await response.text());
  });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  owner = listing.data.active_workspace;
  const workspace = await api('/api/v1/workspace');
  assert.equal(owner.gateway_id, health.data.gateway_id); assert.equal(owner.workspace_id, workspace.data.workspace_id);
  result.initialization = { health, listing, workspace };
  const label = listing.data.items.find(item => item.workspace_id === owner.workspace_id).name;
  const baseline = (await api(sourcePath)).data;
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await phase('original-gallery-install');
  await command('workbench.action.openSettingsJson');
  const settings = page.locator('.monaco-editor').filter({ visible: true }).last();
  await expect(settings.locator('.view-lines')).toBeVisible();
  await settings.locator('.view-lines').click(); await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText(JSON.stringify({ 'chat.mcp.gallery.serviceUrl': fixture.registryUrl }, null, 2) + '\n');
  await command('workbench.action.files.save');
  const dialog = page.getByRole('dialog', { name: /settings\.json$/ });
  if (await dialog.isVisible()) await page.keyboard.press('Escape');
  else await command('workbench.action.closeActiveEditor');
  const fetched = page.waitForResponse(response => response.url().startsWith(fixture.registryUrl)
    && new URL(response.url()).searchParams.get('search') === 'artifact');
  await Promise.all([fetched, command('workbench.extensions.search', '@mcp artifact')]);
  const list = page.locator('.part.sidebar .mcp-servers-list').filter({ visible: true });
  await expect(list.getByText('Artifact Server', { exact: true })).toBeVisible();
  await list.getByText('Artifact Server', { exact: true }).click();
  const detail = page.locator('.mcp-server-editor').filter({ has: page.getByRole('heading', { name: 'Artifact Server', exact: true }) }).filter({ visible: true });
  const install = detail.getByRole('button', { name: 'Install', exact: true });
  const quick = page.locator('.quick-input-widget');
  const pick = async text => quick.locator('.monaco-list-row').filter({ has: page.getByText(text, { exact: true }) }).click();
  const publish = async () => {
    await expect(install).toBeEnabled(); await install.click();
    await expect(quick.locator('.quick-input-title')).toHaveText('安装 MCP 制品至 本地');
    await pick('pypi: artifact-server 1.0.0');
    await expect(quick).toContainText('输入根 distribution 的 console entry point');
    await quick.locator('input').fill('artifact-server'); await page.keyboard.press('Enter');
    await expect(page.locator('.notifications-toasts')).toContainText('尚未配置或连接工作区', { timeout: 40_000 });
    await Promise.all(pending);
    const completed = result.responses.filter(item => item.body.data?.status === 'succeeded').at(-1);
    assert(completed?.body.data.artifact, '安装必须由原操作确认发布');
    return completed.body.data.artifact;
  };
  const configure = async enabled => {
    await page.locator('.notifications-toasts').getByRole('button', { name: '选择工作区配置', exact: true }).click();
    await expect(quick.locator('.quick-input-title')).toHaveText('选择 MCP 配置工作区');
    await pick(label);
    await expect(quick.locator('.quick-input-title')).toHaveText('配置已发布 MCP 制品');
    await quick.locator('input').fill('configured-server'); await page.keyboard.press('Enter');
    await expect(quick.locator('.quick-input-title')).toHaveText('选择 configured-server 的启用状态');
    await pick(enabled ? '启用此 Server' : '暂不启用此 Server');
    await expect(quick.locator('.quick-input-title')).toHaveText('确认 MCP 工作区配置提案');
    await expect(quick).toContainText('workspace_root_mutable_override');
    await expect(quick).toContainText('configured-server');
    await expect(quick.locator('.quick-input-message')).toContainText(owner.gateway_id);
    await expect(quick.locator('.quick-input-message')).toContainText(owner.workspace_id);
    await expect(quick.locator('.quick-input-message')).toContainText('Backend');
    await expect(quick.locator('.quick-input-message')).toContainText('变更：');
  };
  await publish();
  assert.equal((await api(sourcePath)).data.file_digest, baseline.file_digest);
  assert.equal(result.drafts.length, 0); assert.equal(result.saves.length, 0);
  result.publishedWithoutConfiguration = true;
  await phase('cancelled-draft-confirmation');
  await configure(false);
  await page.screenshot({ path: join(fixture.artifacts, 'configuration-confirmation.png'), fullPage: true });
  await page.keyboard.press('Escape'); await expect(quick).not.toBeVisible();
  const unchanged = (await api(sourcePath)).data;
  assert.equal(unchanged.file_digest, baseline.file_digest); assert.equal(unchanged.jsonc, baseline.jsonc);
  assert.equal(result.saves.length, 0); result.cancelledWithoutSave = true;
  await phase('explicit-configuration');
  const artifact = await publish(); result.binding = artifact.binding; result.artifactCommand = artifact.command;
  await configure(fixture.enabled);
  await expect(quick).toContainText(artifact.binding.generation);
  if (fixture.responseLost) {
    await page.route('**/api/v1/mcp/candidate-configurations', async route => {
      const response = await route.fetch();
      const body = await response.json();
      assert.equal(response.status(), 200, JSON.stringify(body)); assert.equal(body.request_id, response.headers()['x-request-id']);
      result.lostCommit = { body, headers: response.headers() };
      await route.abort('failed');
    }, { times: 1 });
  }
  await pick('确认保存工作区配置'); await expect(quick).not.toBeVisible();
  if (fixture.responseLost) {
    // 先等待同一真实 PUT 完成后丢弃回包，再按原 HTTP 15 秒期限观察来源回读与通知。
    await expect.poll(() => Boolean(result.lostCommit), { timeout: 15_000 }).toBe(true);
    await expect(page.locator('.notifications-toasts')).toContainText('保存结果未确认', { timeout: 15_000 });
    await expect(page.locator('.notifications-toasts')).toContainText('不会自动重发');
    assert.equal(result.saves.length, 1);
    const key = result.saves[0].body.source_edit.idempotency_key;
    const queried = page.waitForResponse(response => new URL(response.url()).pathname === sourcePath
      && new URL(response.url()).searchParams.get('idempotency_key') === key);
    await page.locator('.notifications-toasts').getByRole('button', { name: '查询原来源提交', exact: true }).click();
    const response = await queried, body = await response.json();
    assert.equal(body.request_id, response.headers()['x-request-id']); assert.equal(body.data.edit.idempotency_key, key);
    result.queriedOriginalCommit = true;
  }
  await expect(page.locator('.notifications-toasts')).toContainText('source 状态');
  await expect(page.locator('.notifications-toasts')).not.toContainText('MCP ready');
  await Promise.all(pending);
  assert.equal(result.saves.length, 1); assert.equal(result.drafts.length, 2);
  const intent = result.saves[0].body, proposal = result.drafts[1].body;
  const installs = result.requests.filter(item => /^\/api\/gateway\/mcp\/[^/]+\/[^/]+\/installations$/.test(item.path) && item.method === 'POST');
  assert.equal(installs.length, 2);
  for (const item of installs) assert.equal(item.headers['x-boxteam-workspace-id'], undefined);
  const plans = result.requests.filter(item => item.path === '/api/v1/mcp/candidate-configurations/plans');
  assert.equal(plans.length, 2);
  for (const item of plans) assert.equal(item.headers['x-boxteam-workspace-id'], owner.workspace_id);
  const original = installs[1].body.candidate;
  assert.deepEqual(proposal.plan.candidate, { ...original, server_id: 'configured-server' });
  assert.equal(proposal.enabled, fixture.enabled); assert.deepEqual(intent.plan, proposal.plan);
  assert.deepEqual(intent.managed_launch, artifact.binding); assert.deepEqual(intent.target, { kind: 'workspace', ...owner });
  const drafted = result.responses.filter(item => item.path.endsWith('/candidate-configurations/draft')).at(-1).body.data;
  assert.deepEqual(intent.source_edit, drafted.source_edit);
  assert.equal(intent.source_edit.base_file_digest, baseline.file_digest);
  for (const item of [...result.drafts, ...result.saves]) {
    assert.equal(item.headers['x-boxteam-target-gateway-id'], owner.gateway_id); assert.equal(item.headers['x-boxteam-workspace-id'], owner.workspace_id);
  }
  const committed = (await api(sourcePath + '?' + new URLSearchParams({ idempotency_key: intent.source_edit.idempotency_key }))).data;
  assert.equal(committed.edit.idempotency_key, intent.source_edit.idempotency_key); assert.equal(committed.edit.source_saved, true);
  const server = JSON.parse(committed.jsonc).mcp.servers['configured-server'];
  assert.equal(server.enabled, fixture.enabled); assert.deepEqual(server.managed_launch, artifact.binding);
  assert(!result.requests.some(item => item.method !== 'GET' && /\/mcp\/connection-operations|\/config\/sources/.test(item.path)), '候选保存只能经原 MCP PUT，不能额外启动连接或旁路 source writer');
  result.sourceState = committed.edit.state; result.committedSource = committed;
  assert.deepEqual(result.pageErrors, []);
  await page.screenshot({ path: join(fixture.artifacts, 'configuration-complete.png'), fullPage: true });
  await phase('complete');
} catch (error) { failure = error; result.error = String(error?.stack ?? error); }
finally {
  if (failure && page) {
    try { await page.screenshot({ path: join(fixture.artifacts, 'configuration-failure.png'), fullPage: true }); }
    catch (error) { failure = new AggregateError([failure, error], '配置场景与截图均失败'); }
  }
  try { await Promise.all(pending); await saveResult(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '配置场景与证据保存均失败') : error; }
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '配置场景与回收均失败') : error; }
}
if (failure) throw failure;
