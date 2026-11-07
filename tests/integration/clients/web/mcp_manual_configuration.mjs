import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_MCP_MANUAL_CONFIGURATION);
assert(fixture.artifacts && fixture.command && fixture.args && fixture.cwd && fixture.endpoint && fixture.envSecret && fixture.headerSecret,
  '必须显式指定目标启动输入、HTTP endpoint、私有测试值及正式产物目录');
const run = TestRunContext.fromTestFile('tests/integration/gateway/test_mcp_manual_configuration.py');
const result = { phase: 'initialize', pageErrors: [], initialization: [], requests: [], responses: [], privateWrites: [], drafts: [], saves: [],
  boundary: '正式 Workbench／Gateway／Backend；显式 stdio 测试进程及HTTP MCP替身；HTTP保存响应经真实提交后中断，未伪造成功；秘密仅保留引用或脱敏证据' };
const sourcePath = '/api/v1/config/sources/workspace_root_mutable_override';
const savePath = '/api/v1/mcp/candidate-configurations';
const privatePaths = ['/api/v1/mcp/private-env', '/api/v1/mcp/private-headers'];
const secrets = [fixture.envSecret, fixture.headerSecret];
const redact = value => secrets.reduce((text, secret) => text.replaceAll(secret, '<private>'), String(value));
const errorText = error => [error?.stack ?? error,
  ...(error instanceof AggregateError ? error.errors.map(errorText) : []),
  ...(error?.cause ? [errorText(error.cause)] : [])].map(redact).join('\n');
const saveResult = () => writeFile(join(fixture.artifacts, 'manual-configuration.json'), redact(JSON.stringify(result, null, 2)));
const phase = async value => { result.phase = value; await saveResult(); };
const pending = [], observationErrors = [];
let page, failure;
const observe = operation => pending.push(operation.catch(error => { observationErrors.push(error); }));
async function observations() {
  await Promise.all(pending);
  if (observationErrors.length) throw new AggregateError(observationErrors, '手动配置请求观察失败');
}
async function screenshot(name) {
  const exposed = await page.evaluate(values => values.some(secret => document.body.innerText.includes(secret)
    || [...document.querySelectorAll('input')].some(input => input.type !== 'password' && input.value.includes(secret))), secrets);
  assert(!exposed, '存在未遮蔽私有输入，不能保存截图');
  await page.screenshot({ path: join(fixture.artifacts, name), fullPage: true });
}
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('manual-mcp-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 } });
  run.addCleanup('manual-mcp-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(redact(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/v1/mcp/') && !path.startsWith('/api/gateway/mcp/') && !path.startsWith(sourcePath)) return;
    observe((async () => {
      let body = request.postData() ? request.postDataJSON() : undefined;
      if (privatePaths.includes(path)) {
        const secret = path.endsWith('private-env') ? fixture.envSecret : fixture.headerSecret;
        assert(body.value === secret, '私有入口须消费原始交互值');
        const { value: _private, ...definition } = body;
        assert(secrets.every(value => !JSON.stringify(definition).includes(value)), '私有请求的公开身份不能包含原值');
        body = { ...definition, value: '<private>' };
      } else assert(secrets.every(value => !JSON.stringify(body ?? {}).includes(value)), '普通请求不得携带秘密');
      const headers = Object.fromEntries(Object.entries(request.headers()).filter(([key]) =>
        ['x-boxteam-target-gateway-id', 'x-boxteam-workspace-id', 'x-request-id'].includes(key)));
      const item = { path, method: request.method(), body, headers };
      result.requests.push(item);
      if (privatePaths.includes(path)) result.privateWrites.push(item);
      if (path === savePath + '/draft') result.drafts.push(item);
      if (path === savePath && request.method() === 'PUT') result.saves.push(item);
    })());
  });
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (!path.startsWith('/api/v1/mcp/') && !path.startsWith('/api/gateway/mcp/') && !path.startsWith(sourcePath)) return;
    observe((async () => {
      const body = await response.json();
      assert(secrets.every(value => !JSON.stringify(body).includes(value)), '公开响应不得返回秘密');
      assert.equal(body.request_id, response.headers()['x-request-id']);
      assert.equal(response.status(), 200, JSON.stringify(body));
      result.responses.push({ path, method: response.request().method(), body });
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
      body: JSON.stringify({ tracking: { source: 'manual-mcp-configuration' } }) });
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
  const quick = page.locator('.quick-input-widget');
  const title = text => expect(quick.locator('.quick-input-title')).toHaveText(text);
  const pick = async text => {
    const row = quick.locator('.monaco-list-row').filter({ has: page.getByText(text, { exact: true }) });
    await expect(row).toBeVisible(); await row.click();
  };
  const enter = async (text, value, password = false) => {
    await title(text); await expect(quick.locator('input')).toHaveAttribute('type', password ? 'password' : 'text');
    await quick.locator('input').fill(value); await page.keyboard.press('Enter');
  };
  const add = async (serverId, viaList = false) => {
    await page.keyboard.press('F1');
    await quick.locator('input').fill(viaList ? '>MCP: List Servers' : '>MCP: Add Server');
    await pick(viaList ? 'MCP: List Servers' : 'MCP: Add Server...');
    if (viaList) await pick('Add Server');
    await title('选择 MCP 配置工作区'); await pick(label);
    await enter('添加 MCP Server', serverId);
  };
  const env = async (key, value, privateValue = false) => {
    await title('配置 MCP 环境变量'); await pick(`添加${privateValue ? '私有' : '普通'}环境变量`);
    await enter('环境变量名称', key);
    await enter(`${privateValue ? '私有' : '普通'}环境变量值 · ${key}`, value, privateValue);
  };
  const header = async (key, value, privateValue = false) => {
    await title('配置 MCP HTTP Header'); await pick(`添加${privateValue ? '私有' : '普通'}HTTP Header`);
    await enter('HTTP Header名称', key);
    await enter(`${privateValue ? '私有' : '普通'}HTTP Header值 · ${key}`, value, privateValue);
  };
  await phase('cancel-before-private-write');
  await add('cancelled-http', true); await pick('Streamable HTTP');
  await enter('MCP Streamable HTTP endpoint', fixture.endpoint);
  await header('X-Private-Token', fixture.headerSecret, true); await pick('完成HTTP Header');
  await title('选择 cancelled-http 的启用状态'); await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible(); await observations();
  const cancelled = (await api(sourcePath)).data;
  assert.equal(cancelled.file_digest, baseline.file_digest); assert.equal(cancelled.jsonc, baseline.jsonc);
  assert.equal(result.privateWrites.length, 0); assert.equal(result.drafts.length, 0); assert.equal(result.saves.length, 0);
  result.cancelledWithoutPrivateWrite = true;

  await phase('manual-stdio-configuration');
  await add('manual-stdio'); await pick('stdio');
  await enter('MCP stdio 可执行命令', fixture.command);
  for (const [index, value] of fixture.args.entries()) { await pick('添加一个参数'); await enter(`MCP 参数 #${index + 1}`, value); }
  await pick('完成参数'); await pick('指定工作目录'); await enter('MCP stdio 工作目录', fixture.cwd);
  await env('MCP_ENV_RECEIPTS', fixture.receipts); await env('MCP_PLAIN_VALUE', 'ordinary'); await env('MCP_EMPTY_VALUE', '');
  await env('MCP_PRIVATE_TOKEN', fixture.envSecret, true); await pick('完成环境变量');
  await pick('启用此 Server'); await title('确认 MCP 工作区配置提案');
  for (const text of [owner.gateway_id, owner.workspace_id, 'Backend', '变更：', '取消保存不会删除该版本']) await expect(quick).toContainText(text);
  await screenshot('manual-stdio-confirmation.png'); await pick('确认保存工作区配置');
  await expect(quick).not.toBeVisible(); await expect(page.locator('.notifications-toasts')).toContainText('source 状态');
  await observations();
  assert.equal(result.privateWrites.length, 1); assert.equal(result.drafts.length, 1); assert.equal(result.saves.length, 1);
  const stdioSource = (await api(sourcePath)).data;
  assert.equal(stdioSource.effective.mcp.servers['manual-stdio'].enabled, true);
  const stdioIntent = result.saves[0].body, stdioCandidate = stdioIntent.plan.candidate;
  const stdioReference = result.responses.find(item => item.path === privatePaths[0]).body.data;
  assert.equal(stdioReference.kind, 'mcp_private_env'); assert.equal(stdioReference.workspace_id, owner.workspace_id);
  assert.deepEqual(stdioCandidate.source.definition, { transport: 'stdio', command: fixture.command, args: fixture.args, cwd: fixture.cwd,
    env: { MCP_ENV_RECEIPTS: fixture.receipts, MCP_PLAIN_VALUE: 'ordinary', MCP_EMPTY_VALUE: '', MCP_PRIVATE_TOKEN: stdioReference } });
  assert.equal(stdioIntent.source_edit.base_file_digest, baseline.file_digest);
  result.stdioConfiguration = stdioSource;

  await phase('same-id-rejection');
  await add('manual-stdio'); await expect(quick).not.toBeVisible();
  await expect(page.locator('.notifications-toasts')).toContainText('Add 不会覆盖已有配置');
  await observations(); assert.equal(result.privateWrites.length, 1); assert.equal(result.drafts.length, 1); assert.equal(result.saves.length, 1);
  assert.equal((await api(sourcePath)).data.file_digest, stdioSource.file_digest);
  result.sameIdRejectedWithoutUpsert = true;

  await phase('http-save-response-lost');
  let intercepted = 0;
  const loseResponse = async route => {
    if (route.request().method() !== 'PUT') return route.continue();
    ++intercepted;
    const response = await route.fetch();
    const body = await response.json();
    assert.equal(response.status(), 200); assert.equal(body.request_id, response.headers()['x-request-id']);
    assert.equal(body.data.edit.source_saved, true);
    assert(secrets.every(value => !JSON.stringify(body).includes(value)));
    result.lostSaveReceipt = body;
    await route.abort('failed');
  };
  await page.route('**' + savePath, loseResponse);
  await add('manual-http'); await pick('Streamable HTTP'); await enter('MCP Streamable HTTP endpoint', fixture.endpoint);
  await header('X-Plain-Test', 'ordinary-header'); await header('X-Private-Token', fixture.headerSecret, true);
  await pick('完成HTTP Header'); await pick('启用此 Server'); await title('确认 MCP 工作区配置提案');
  await screenshot('manual-http-confirmation.png'); await pick('确认保存工作区配置'); await expect(quick).not.toBeVisible();
  await expect(page.locator('.notifications-toasts')).toContainText('保存结果未确认，不会自动重发');
  await observations(); assert.equal(intercepted, 1); assert.equal(result.saves.length, 2);
  await page.locator('.notifications-toasts').getByRole('button', { name: '查询原来源提交', exact: true }).click();
  const httpKey = result.saves[1].body.source_edit.idempotency_key;
  await expect(page.locator('.notifications-toasts')).toContainText(`提交 ${httpKey}`);
  await page.unroute('**' + savePath, loseResponse); await observations();
  const committed = (await api(sourcePath + '?' + new URLSearchParams({ idempotency_key: httpKey }))).data;
  assert.equal(committed.edit.idempotency_key, httpKey); assert.equal(committed.edit.source_saved, true);
  const httpReference = result.responses.find(item => item.path === privatePaths[1]).body.data;
  assert.equal(httpReference.kind, 'mcp_private_header'); assert.equal(httpReference.workspace_id, owner.workspace_id);
  assert.equal(httpReference.endpoint, fixture.endpoint); assert.equal(httpReference.server_id, 'manual-http');
  assert.deepEqual(result.saves[1].body.plan.candidate.source.definition,
    { transport: 'streamable_http', url: fixture.endpoint, headers: { 'X-Plain-Test': 'ordinary-header', 'X-Private-Token': httpReference } });
  assert.equal(result.saves[1].body.source_edit.base_file_digest, stdioSource.file_digest);
  for (const request of [...result.privateWrites, ...result.drafts, ...result.saves,
    ...result.requests.filter(item => item.path === savePath + '/plans')]) {
    assert.equal(request.headers['x-boxteam-target-gateway-id'], owner.gateway_id);
    assert.equal(request.headers['x-boxteam-workspace-id'], owner.workspace_id);
  }
  for (const request of [...result.drafts, ...result.saves]) {
    assert.equal(request.body.managed_launch, undefined); assert.deepEqual(request.body.target, { kind: 'workspace', ...owner });
  }
  assert.equal(result.privateWrites.length, 2); assert.equal(result.drafts.length, 2); assert.equal(result.saves.length, 2);
  assert.equal(result.requests.filter(item => item.path === savePath + '/plans').length, 2);
  assert.equal(result.requests.filter(item => item.path.endsWith('/candidate-manifests')).length, 2);
  assert(!result.requests.some(item => item.method !== 'GET' && /\/installations$|\/connection-operations|\/config\/sources\//.test(item.path)),
    '手动定义不得进入包安装、额外连接操作或旁路 source writer');
  result.httpConfiguration = committed; result.unknownSaveQueriedWithoutReplay = true;
  await observations(); assert.deepEqual(result.pageErrors, []);
  await screenshot('manual-configuration-complete.png'); await phase('complete');
} catch (error) { failure = error; result.error = errorText(error); }
finally {
  if (failure && page) {
    try { await screenshot('manual-configuration-failure.png'); }
    catch (error) { failure = new AggregateError([failure, error], '手动配置与截图均失败'); }
  }
  try { await observations(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '手动配置与观察均失败') : error; }
  if (failure) result.error = errorText(failure);
  try { await saveResult(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '手动配置与证据保存均失败') : error; }
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '手动配置与回收均失败') : error; }
}
if (failure) throw new Error(errorText(failure));
