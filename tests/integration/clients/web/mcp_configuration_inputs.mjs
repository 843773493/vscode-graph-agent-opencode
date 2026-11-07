import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_MCP_CONFIGURATION_INPUTS);
assert(fixture.artifacts && fixture.registryUrl && fixture.secretKey && fixture.secretValue && fixture.expectedInputs,
  '必须显式指定原目录、输入、私有测试值与正式产物目录');
const run = TestRunContext.fromTestFile('tests/integration/gateway/test_mcp_configuration_inputs.py');
const result = { phase: 'initialize', pageErrors: [], initialization: [], requests: [], responses: [], drafts: [], saves: [], privateWrites: [],
  boundary: '真实 Workbench／Gateway／Backend／uv；本地 registry 与 wheel 为受控输入；秘密请求仅保存脱敏证据；进程实际参数与环境由 Python 后续真实 Thread 验证' };
const sourcePath = '/api/v1/config/sources/workspace_root_mutable_override';
const privatePath = '/api/v1/mcp/candidate-plans/private-env';
const serverId = 'configured-inputs';
const pending = [], observationErrors = [];
let page, failure;
const redact = value => String(value).replaceAll(fixture.secretValue, '<private>');
const errorText = error => [error?.stack ?? error,
  ...(error instanceof AggregateError ? error.errors.map(errorText) : []),
  ...(error?.cause ? [errorText(error.cause)] : [])].map(redact).join('\n');
const saveResult = () => writeFile(join(fixture.artifacts, 'configuration-inputs.json'), redact(JSON.stringify(result, null, 2)));
const phase = async value => { result.phase = value; await saveResult(); };
const observe = operation => pending.push(operation.catch(error => { observationErrors.push(error); }));
async function observations() {
  await Promise.all(pending);
  if (observationErrors.length) throw new AggregateError(observationErrors, '配置输入请求观察失败');
}
async function screenshot(name) {
  const exposed = await page.evaluate(secret => document.body.innerText.includes(secret)
    || [...document.querySelectorAll('input')].some(input => input.type !== 'password' && input.value.includes(secret)), fixture.secretValue);
  assert(!exposed, '页面出现未遮蔽的私有输入，不能保存截图');
  await page.screenshot({ path: join(fixture.artifacts, name), fullPage: true });
}
async function command(id, ...args) {
  await page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
}
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('configuration-inputs-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('configuration-inputs-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(redact(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/v1/') && !path.startsWith('/api/gateway/mcp/')) return;
    observe((async () => {
      let body = request.postData() ? request.postDataJSON() : undefined;
      if (path === privatePath) {
        assert(body.value === fixture.secretValue, '私有env请求必须消费原输入');
        const { value: _private, ...definition } = body;
        assert(!JSON.stringify(definition).includes(fixture.secretValue));
        body = { ...definition, value: '<private>' };
      } else assert(!JSON.stringify(body ?? {}).includes(fixture.secretValue), '普通请求不能携带秘密明文');
      const headers = Object.fromEntries(Object.entries(request.headers()).filter(([key]) =>
        ['x-boxteam-target-gateway-id', 'x-boxteam-workspace-id', 'x-request-id'].includes(key)));
      const item = { path, url: request.url(), method: request.method(), body, headers };
      result.requests.push(item);
      if (path === privatePath) result.privateWrites.push(item);
      if (path === '/api/v1/mcp/candidate-configurations/draft') result.drafts.push(item);
      if (path === '/api/v1/mcp/candidate-configurations' && request.method() === 'PUT') result.saves.push(item);
    })());
  });
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (!/\/mcp\/(?:[^/]+\/[^/]+\/)?(installation|candidate)/.test(path) && !path.startsWith(sourcePath)) return;
    observe((async () => {
      const body = await response.json();
      assert(!JSON.stringify(body).includes(fixture.secretValue), '响应不能返回秘密明文');
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
      body: JSON.stringify({ tracking: { source: 'mcp-configuration-inputs' } }) });
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
  const galleryName = fixture.galleryName ?? 'Artifact Server';
  const list = page.locator('.part.sidebar .mcp-servers-list').filter({ visible: true });
  await expect(list.getByText(galleryName, { exact: true })).toBeVisible();
  await list.getByText(galleryName, { exact: true }).click();
  const detail = page.locator('.mcp-server-editor').filter({ has: page.getByRole('heading', { name: galleryName, exact: true }) }).filter({ visible: true });
  const install = detail.getByRole('button', { name: 'Install', exact: true });
  const quick = page.locator('.quick-input-widget');
  const pick = async text => {
    const row = quick.locator('.monaco-list-row').filter({ has: page.getByText(text, { exact: true }) });
    await expect(row).toBeVisible(); await row.click();
  };
  const title = text => expect(quick.locator('.quick-input-title')).toHaveText(text);
  const enter = async (key, value) => {
    await title(`配置 MCP 输入 ${key}`);
    await expect(quick.locator('input')).toHaveAttribute('type', 'text');
    await quick.locator('input').fill(value); await page.keyboard.press('Enter');
  };
  const publish = async () => {
    await expect(install).toBeEnabled(); await install.click();
    await title('安装 MCP 制品至 本地');
    await pick(fixture.variantLabel ?? 'pypi: artifact-server 1.0.0');
    await expect(quick).toContainText('输入根 distribution 的 console entry point');
    await quick.locator('input').fill(fixture.entrypoint ?? 'artifact-server'); await page.keyboard.press('Enter');
    await expect(page.locator('.notifications-toasts')).toContainText('尚未配置或连接工作区', { timeout: 40_000 });
    await observations();
    const completed = result.responses.filter(item => item.body.data?.status === 'succeeded').at(-1);
    assert(completed?.body.data.artifact, '安装必须由原操作确认发布');
    return completed.body.data.artifact;
  };
  const collect = async cancel => {
    await page.locator('.notifications-toasts').getByRole('button', { name: '选择工作区配置', exact: true }).click();
    await title('选择 MCP 配置工作区'); await pick(label);
    await title('配置已发布 MCP 制品');
    await quick.locator('input').fill(serverId); await page.keyboard.press('Enter');
    await title(`选择 ${serverId} 的启用状态`); await pick('启用此 Server');
    await title('配置 MCP 输入 --tag');
    for (const value of ['red', 'red', 'blue']) { await pick('添加一个值'); await pick(value); }
    await pick('完成输入');
    await title('配置 MCP 输入 --port'); await pick('使用默认值');
    await title('配置 MCP 输入 --boolean'); await pick('true');
    await title('配置 MCP 输入 --directory'); await pick('输入值'); await enter('--directory', '/native target/with spaces');
    await title('配置 MCP 输入 --optional'); await pick('跳过此输入');
    await title('配置 MCP 输入 --empty'); await pick('输入值'); await enter('--empty', '');
    await enter('label', '');
    await title('配置 MCP 输入 MCP_PLAIN_VALUE'); await pick('输入值'); await pick('fast');
    await title('配置 MCP 输入 MCP_EMPTY_VALUE'); await pick('输入值'); await enter('MCP_EMPTY_VALUE', '');
    await title('配置 MCP 输入 MCP_SKIPPED_VALUE'); await pick('跳过此输入');
    await title(`配置 MCP 输入 ${fixture.secretKey}`);
    await expect(quick.locator('input')).toHaveAttribute('type', 'password');
    await quick.locator('input').fill(fixture.secretValue);
    await page.keyboard.press(cancel ? 'Escape' : 'Enter');
  };
  await publish();
  assert.equal((await api(sourcePath)).data.file_digest, baseline.file_digest);
  assert.equal(result.privateWrites.length, 0); assert.equal(result.drafts.length, 0); assert.equal(result.saves.length, 0);
  result.publishedWithoutConfiguration = true;
  await phase('cancelled-input-collection');
  await collect(true); await expect(quick).not.toBeVisible(); await observations();
  const unchanged = (await api(sourcePath)).data;
  assert.equal(unchanged.file_digest, baseline.file_digest); assert.equal(unchanged.jsonc, baseline.jsonc);
  assert.equal(result.privateWrites.length, 0); assert.equal(result.drafts.length, 0); assert.equal(result.saves.length, 0);
  assert.equal(result.requests.filter(item => item.path === '/api/v1/mcp/candidate-configurations/plans').length, 0);
  result.cancelledWithoutPrivateWrite = true;
  await phase('explicit-input-configuration');
  const artifact = await publish(); result.binding = artifact.binding; result.artifactCommand = artifact.command;
  await collect(false);
  await title('确认 MCP 工作区配置提案');
  for (const text of [owner.gateway_id, owner.workspace_id, 'Backend', '变更：', artifact.binding.generation]) await expect(quick).toContainText(text);
  await screenshot('configuration-inputs-confirmation.png');
  await pick('确认保存工作区配置'); await expect(quick).not.toBeVisible();
  await expect(page.locator('.notifications-toasts')).toContainText('source 状态');
  await observations();
  assert.equal(result.privateWrites.length, 1); assert.equal(result.drafts.length, 1); assert.equal(result.saves.length, 1);
  const privateWrite = result.privateWrites[0], intent = result.saves[0].body, proposal = result.drafts[0].body;
  const privateReceipt = result.responses.find(item => item.path === privatePath).body.data;
  assert.equal(privateReceipt.kind, 'mcp_private_env'); assert.equal(privateReceipt.workspace_id, owner.workspace_id);
  assert.equal(privateReceipt.server_id, serverId); assert.equal(privateReceipt.input_key, fixture.secretKey);
  assert.match(privateReceipt.definition_digest, /^sha256:jcs:v1:[0-9a-f]{64}$/); assert.match(privateReceipt.version, /^[0-9a-f]{32}$/);
  const expected = { ...fixture.expectedInputs, [fixture.secretKey]: privateReceipt };
  const plans = result.requests.filter(item => item.path === '/api/v1/mcp/candidate-configurations/plans');
  assert.equal(plans.length, 1); assert.deepEqual(plans[0].body.inputs, expected);
  assert.deepEqual(proposal.plan.candidate.inputs, expected); assert.deepEqual(intent.plan, proposal.plan);
  assert.equal(proposal.enabled, true); assert.deepEqual(intent.managed_launch, artifact.binding);
  assert.deepEqual(intent.target, { kind: 'workspace', ...owner });
  assert.equal(privateWrite.body.input_key, fixture.secretKey); assert.equal(privateWrite.body.server_id, serverId);
  assert.deepEqual(privateWrite.body.target, { kind: 'workspace', ...owner });
  assert.deepEqual(privateWrite.body.candidate.inputs, fixture.expectedInputs);
  assert.equal(result.requests.indexOf(privateWrite) < result.requests.indexOf(plans[0]), true);
  const drafted = result.responses.find(item => item.path.endsWith('/candidate-configurations/draft')).body.data;
  assert.deepEqual(intent.source_edit, drafted.source_edit); assert.equal(intent.source_edit.base_file_digest, baseline.file_digest);
  for (const item of [privateWrite, ...plans, ...result.drafts, ...result.saves]) {
    assert.equal(item.headers['x-boxteam-target-gateway-id'], owner.gateway_id); assert.equal(item.headers['x-boxteam-workspace-id'], owner.workspace_id);
  }
  const installs = result.requests.filter(item => /^\/api\/gateway\/mcp\/[^/]+\/[^/]+\/installations$/.test(item.path) && item.method === 'POST');
  assert.equal(installs.length, 2);
  for (const item of installs) { assert.deepEqual(item.body.candidate.inputs, {}); assert.equal(item.headers['x-boxteam-workspace-id'], undefined); }
  assert.deepEqual(proposal.plan.candidate, { ...installs[1].body.candidate, server_id: serverId, inputs: expected });
  const committed = (await api(sourcePath + '?' + new URLSearchParams({ idempotency_key: intent.source_edit.idempotency_key }))).data;
  assert.equal(committed.edit.idempotency_key, intent.source_edit.idempotency_key); assert.equal(committed.edit.source_saved, true);
  const server = JSON.parse(committed.jsonc).mcp.servers[serverId];
  assert.equal(server.enabled, true); assert.deepEqual(server.managed_launch, artifact.binding);
  assert(!JSON.stringify(committed).includes(fixture.secretValue));
  assert(!result.requests.some(item => item.method !== 'GET' && /\/mcp\/connection-operations|\/config\/sources/.test(item.path)),
    '候选配置只能经原 MCP PUT，不能旁路 source writer 或额外启动连接');
  result.sourceState = committed.edit.state; result.committedSource = committed; result.privateReference = privateReceipt;
  await observations();
  assert.deepEqual(result.pageErrors, []);
  await screenshot('configuration-inputs-complete.png');
  await phase('complete');
} catch (error) { failure = error; result.error = errorText(error); }
finally {
  if (failure && page) {
    try { await screenshot('configuration-inputs-failure.png'); }
    catch (error) { failure = new AggregateError([failure, error], '配置输入场景与截图均失败'); }
  }
  try { await observations(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '配置输入场景与请求观察均失败') : error; }
  if (failure) result.error = errorText(failure);
  try { await saveResult(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '配置输入场景与证据保存均失败') : error; }
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '配置输入场景与回收均失败') : error; }
}
if (failure) throw new Error(errorText(failure));
