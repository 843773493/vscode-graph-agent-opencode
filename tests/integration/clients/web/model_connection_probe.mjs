import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_MODEL_PROBE_FIXTURE);
assert(fixture.artifacts && fixture.visibleMarker, '必须显式指定测试产物目录');
const result = { phase: 'initialization', pageErrors: [], requests: [], responses: [], initialization: [] };
const failures = [];
let browser, context, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/v1/config/')) result.requests.push({ path: url.pathname, method: request.method(), body: request.postDataJSON() });
  });
  page.on('response', response => {
    if (new URL(response.url()).pathname.startsWith('/api/v1/config/llm/')) result.responses.push({ url: response.url(), status: response.status(), requestId: response.headers()['x-request-id'] });
  });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30_000 });
  await page.waitForFunction(() => Boolean(globalThis.workbench?.commands?.executeCommand));
  result.initialization = await page.evaluate(async fixture => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const receipts = [];
    for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
      const response = await fetch(url, { headers: { 'X-Local-Token': token,
        'X-BoxTeam-Target-Gateway-Id': fixture.gatewayId, 'X-BoxTeam-Workspace-Id': fixture.workspaceId } });
      const envelope = await response.json();
      if (!response.ok || !envelope.request_id || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
      receipts.push({ url, requestId: envelope.request_id });
    }
    return receipts;
  }, fixture);
  const location = { version: 2, gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
    source_key: 'workspace_root_mutable_override', name: '模型探测工作区' };
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  await command('boxteam.settings.modelsProviders', location, 'primary', 'default');
  const form = page.locator('.configuration-entities[data-definition-kind="providers"]');
  const panel = form.getByRole('region', { name: '发现与测试模型', exact: true });
  await expect(panel).toBeVisible();
  await panel.getByText('测试选项', { exact: true }).click();
  assert.equal(result.requests.filter(item => item.path.includes('/llm/')).length, 0, '打开设置不能探测');
  result.phase = 'connection-commit';
  const endpoint = form.getByLabel('API 地址').first();
  await endpoint.fill(fixture.endpoint + '/');
  const [discovered] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/config/llm/discover'),
    panel.getByRole('button', { name: '刷新观察版本', exact: true }).focus(),
  ]);
  assert.equal(discovered.status(), 200, await discovered.text());
  const connectionDraft = JSON.parse(result.requests.find(item => item.path.endsWith('/llm/discover')).body.jsonc);
  assert.equal(connectionDraft.llm.providers.primary.defaults.endpoint, fixture.endpoint + '/');
  assert.equal(connectionDraft.llm.providers.primary.models.default.overrides?.endpoint, undefined);
  await expect(panel.getByText('完整目录：2 个模型', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '获取上游模型', exact: true }).focus();
  await panel.getByRole('button', { name: '刷新观察版本', exact: true }).focus();
  assert.equal(result.requests.filter(item => item.path.includes('/llm/discover')).length, 1, '纯焦点变化不能重复发现');
  result.phase = 'draft-import';
  await panel.getByLabel('搜索上游模型', { exact: true }).fill('upstream-new');
  await panel.getByRole('button', { name: '选择全部匹配项', exact: true }).click();
  await panel.getByRole('button', { name: '添加选中的模型', exact: true }).click();
  await expect(form.getByRole('button', { name: '目录新模型', exact: true })).toBeVisible();
  assert.equal(result.requests.filter(item => item.method === 'PUT').length, 0);
  result.phase = 'schema-options-edit-original-draft';
  const addParameter = async (options, name, value) => {
    await options.getByLabel('参数名称', { exact: true }).fill(name);
    await options.getByLabel('新参数的 JSON 值', { exact: true }).fill(JSON.stringify(value));
    await options.getByRole('button', { name: '添加请求参数', exact: true }).click();
  };
  const providerOptions = form.locator('.configuration-model-options').first();
  await providerOptions.locator('summary').click();
  await addParameter(providerOptions, 'temperature', 0);
  await addParameter(providerOptions, 'top_p', null);
  await addParameter(providerOptions, 'extra_body', { extension_switch: false, extension_value: null });
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/discover')).length, 1, '参数编辑不能自动探测');
  result.phase = 'inspect-original-model-draft';
  await form.locator('[data-model-id="default"] > details > summary').click();
  const inspection = form.locator('.configuration-model-inspection');
  const inspect = async (expectedStatus = 200) => {
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/preflight')),
      inspection.getByRole('button', { name: '预检并查看最终选项', exact: true }).click(),
    ]);
    assert.equal(response.status(), expectedStatus, await response.text());
    if (expectedStatus !== 200) return response.json();
    const preflight = (await response.json()).data;
    const receipt = preflight.model_options;
    assert.deepEqual(receipt.model_ref, { provider_id: 'primary', model_id: 'default' });
    await expect(inspection.getByRole('status')).toHaveText('本次草稿预检通过');
    await expect(inspection.locator('[data-model-options="draft"] pre')).toHaveText(JSON.stringify(receipt.options, null, 2));
    await expect(inspection.locator('[data-model-options="active"] pre')).toHaveText(JSON.stringify(preflight.active_model_options.options, null, 2));
    assert.equal(preflight.active_model_options.options.request_options.parameters?.temperature, undefined,
      '未保存草稿不能冒充已生效参数');
    assert.equal(result.requests.filter(item => item.method === 'PUT').length, 0);
    return receipt;
  };
  const providerReceipt = await inspect();
  assert.equal(providerReceipt.options.request_options.parameters.temperature, 0);
  assert.equal(providerReceipt.options.request_options.parameters.top_p, null);
  assert.equal(providerReceipt.request_url, fixture.endpoint + '/chat/completions');
  const temperatureOrigin = receipt => receipt.origins.find(origin => origin.path === '/request_options/parameters/temperature');
  assert.equal(temperatureOrigin(providerReceipt).definition, 'defaults');
  assert.equal(temperatureOrigin(providerReceipt).source_key, location.source_key);
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/test')).length, 0, '选项预检不能调用模型');
  result.phase = 'restore-model-credential-in-original-draft';
  const modelCredential = form.locator('[data-model-id="default"] .configuration-credential');
  await modelCredential.getByRole('button', { name: '设置 API 密钥', exact: true }).click();
  await page.locator('.quick-input-widget').getByText('无认证', { exact: true }).click();
  const overriddenCredential = await inspect();
  assert.equal(overriddenCredential.options.api_key, '');
  assert.equal(overriddenCredential.origins.find(origin => origin.path === '/api_key').definition, 'overrides');
  await modelCredential.getByRole('button', { name: '使用服务默认设置', exact: true }).click();
  const inheritedCredential = await inspect();
  assert.equal(inheritedCredential.options.api_key, providerReceipt.options.api_key);
  assert.equal(inheritedCredential.origins.find(origin => origin.path === '/api_key').definition, 'defaults');
  const restoredDraft = JSON.parse(result.requests.filter(item => item.path.endsWith('/preflight')).at(-1).body.jsonc);
  assert.equal(restoredDraft.llm.providers.primary.models.default?.overrides?.api_key, undefined,
    '恢复继承只删除当前Model字段，不复制Provider值或写入null');
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/discover')).length, 1,
    '模型凭据覆盖与恢复不编辑Provider连接');
  result.phase = 'model-protocol-and-streaming-inheritance';
  const modelSection = form.locator('[data-model-id="default"]');
  const modelProtocol = modelSection.getByRole('combobox', { name: /^协议/ });
  const modelStreaming = modelSection.getByRole('combobox', { name: /^响应方式/ });
  await modelStreaming.selectOption('false');
  await modelProtocol.selectOption('responses');
  const incompleteProtocol = await inspect(422);
  assert.match(incompleteProtocol.detail, /model_info\.supports_reasoning/,
    '协议切换不猜测能力，继承的reasoning声明与显式空块冲突时必须拒绝');
  const modelOptions = modelSection.locator('.configuration-model-options');
  await modelOptions.locator('summary').click();
  const reasoningCapability = modelOptions.getByRole('combobox', { name: /^supports_reasoning/ });
  await reasoningCapability.selectOption('false');
  const responseOptions = await inspect();
  assert.equal(responseOptions.request_url, fixture.endpoint + '/responses');
  assert.equal(responseOptions.options.api_mode.streaming, false);
  assert.deepEqual(responseOptions.options.api_mode.supports_reasoning, {});
  await modelProtocol.selectOption('');
  await reasoningCapability.selectOption('');
  const chatOptions = await inspect();
  assert.equal(chatOptions.request_url, fixture.endpoint + '/chat/completions');
  assert.equal(chatOptions.options.api_mode.streaming, false);
  const restoredModeDraft = JSON.parse(result.requests.filter(item => item.path.endsWith('/preflight')).at(-1).body.jsonc);
  const restoredMode = restoredModeDraft.llm.providers.primary.models.default.overrides.api_mode;
  assert.equal(restoredMode.protocol, undefined, '协议恢复继承只删除本层协议');
  assert.equal(restoredMode.supports_reasoning, undefined, '协议恢复继承移除本层reasoning块');
  assert.equal(restoredMode.model_info?.supports_reasoning, undefined, '能力恢复继承删除本层声明');
  assert.equal(restoredMode.streaming, false, '恢复协议和能力不删除独立传输选择');
  await modelStreaming.selectOption('');
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/discover')).length, 1);
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/test')).length, 0);
  result.phase = 'stream-before-terminal';
  await panel.getByRole('button', { name: '测试流式', exact: true }).click();
  const stream = panel.locator('.configuration-probe-result').filter({ hasText: '测试流式' });
  await expect(stream.locator('pre')).toHaveText('PROBE_VISIBLE_BEFORE_TERMINAL', { timeout: 30_000 });
  await expect(stream.getByRole('status')).toContainText('进行中');
  await writeFile(fixture.visibleMarker, JSON.stringify({ visibleBeforeTerminal: true, observedAt: Date.now() }) + '\n');
  await expect(stream.getByRole('status')).toContainText('本次通过');
  result.phase = 'nonstream-real-http';
  await addParameter(modelOptions, 'temperature', 0.25);
  await modelOptions.locator('[data-parameter="temperature"]').getByRole('button', { name: '不发送此参数', exact: true }).click();
  await modelOptions.getByRole('button', { name: '清空推理格式', exact: true }).click();
  const wireForm = modelOptions.locator('.configuration-option-json').filter({ has: page.getByLabel('api_mode.supports_reasoning', { exact: true }) });
  await wireForm.getByRole('button', { name: '清除设置', exact: true }).click();
  await expect(inspection.getByRole('status')).toContainText('草稿已改变');
  const modelReceipt = await inspect();
  assert.equal(modelReceipt.options.request_options.parameters.temperature, null);
  assert.equal(temperatureOrigin(modelReceipt).definition, 'overrides');
  assert.equal(temperatureOrigin(modelReceipt).source_key, location.source_key);
  await panel.getByRole('button', { name: '测试非流式', exact: true }).click();
  const complete = panel.locator('.configuration-probe-result').filter({ hasText: '测试非流式' });
  await expect(complete.getByRole('status')).toContainText('本次通过');
  await expect(complete.locator('pre')).toHaveText('PROBE_VISIBLE_BEFORE_TERMINAL');
  const probes = result.requests.filter(item => item.path.endsWith('/llm/test'));
  assert.equal(probes.length, 2);
  assert.deepEqual(probes.map(item => item.body.streaming), [true, false]);
  assert(probes.every(item => item.body.mode === 'draft' && item.body.jsonc.includes('upstream-new')));
  const modelDraft = JSON.parse(probes[1].body.jsonc).llm.providers.primary.models.default.overrides;
  assert.equal(modelDraft.request_options.parameters.temperature, null);
  assert.equal(modelDraft.api_mode?.supports_reasoning, undefined);
  assert.equal(result.requests.filter(item => item.method === 'PUT').length, 0);
  assert(result.responses.every(item => item.status === 200 && item.requestId));
  result.phase = 'same-source-jsonc';
  await command('boxteam.settings.openSource', { location });
  await expect(page.locator('.editor-instance:visible .monaco-editor .view-lines').last()).toContainText('upstream-new');
  result.phase = 'create-provider-without-model';
  await command('boxteam.settings.modelsProviders', location);
  await form.getByRole('button', { name: '添加模型服务', exact: true }).click();
  const quick = page.locator('.quick-input-widget');
  const input = async value => {
    await expect(quick).toBeVisible();
    const control = quick.locator('input').first();
    await expect(control).toBeFocused(); await control.fill(value); await control.press('Enter');
  };
  await input('new-probe');
  await quick.locator('.monaco-list-row').filter({ hasText: 'OpenAI 兼容' }).click();
  await expect(form.locator('[data-definition-id="new-probe"]')).toBeVisible();
  await panel.getByText('测试选项', { exact: true }).click();
  await expect(panel.getByRole('button', { name: '测试配置模式', exact: true })).toBeDisabled();
  const beforeNewConnection = result.requests.filter(item => item.path.endsWith('/llm/discover')).length;
  await form.getByLabel('API 地址').first().fill(fixture.endpoint);
  const [newConnection] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover')),
    panel.getByRole('button', { name: '刷新观察版本', exact: true }).focus(),
  ]);
  assert.equal(newConnection.status(), 200, await newConnection.text());
  const connectionInput = result.requests.filter(item => item.path.endsWith('/llm/discover')).at(-1).body;
  assert.deepEqual(JSON.parse(connectionInput.jsonc).llm.providers['new-probe'].models, {});
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/discover')).length, beforeNewConnection + 1);
  result.phase = 'credential-reference-in-original-draft';
  await form.getByRole('button', { name: '设置 API 密钥', exact: true }).click();
  await quick.locator('.monaco-list-row').filter({ hasText: '新建命名密钥' }).click();
  await input('探测共享密钥');
  const created = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/secrets' && response.request().method() === 'POST');
  const credentialDiscovery = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover'));
  await input('ephemeral-probe-' + randomUUID());
  const key = (await (await created).json()).data.item;
  assert.equal((await credentialDiscovery).status(), 200);
  assert.equal(result.requests.filter(item => item.method === 'PUT').length, 0, '选择引用不能保存脏来源');
  const keyInput = result.requests.filter(item => item.path.endsWith('/llm/discover')).at(-1).body;
  assert.equal(JSON.parse(keyInput.jsonc).llm.providers['new-probe'].defaults.api_key, 'secret:' + key.id);
  await expect(panel.getByText('完整目录：2 个模型', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '选择全部匹配项', exact: true }).click();
  await panel.getByRole('button', { name: '添加选中的模型', exact: true }).click();
  await panel.getByRole('button', { name: '测试两种模式', exact: true }).click();
  const newStream = panel.locator('.configuration-probe-result').filter({ hasText: '测试流式' });
  const newComplete = panel.locator('.configuration-probe-result').filter({ hasText: '测试非流式' });
  await expect(newStream.getByRole('status')).toContainText('本次通过');
  await expect(newComplete.getByRole('status')).toContainText('本次通过');
  await expect(newStream).toContainText('r1');
  await expect(newStream).not.toContainText('已过期');
  result.phase = 'external-key-rotation-and-explicit-refresh';
  const rotated = await page.evaluate(async ({ key, gatewayId }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch('/api/gateway/secrets/' + key.id, { method: 'PUT', headers: {
      'Content-Type': 'application/json', 'X-Local-Token': token, 'X-BoxTeam-Target-Gateway-Id': gatewayId,
    }, body: JSON.stringify({ name: key.name, expected_revision: key.revision, value: 'rotated-fixture-input' }) });
    const envelope = await response.json();
    if (!response.ok || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return envelope.data.items.find(item => item.id === key.id);
  }, { key, gatewayId: fixture.gatewayId });
  assert.equal(rotated.revision, 2);
  await expect(newStream).not.toContainText('已过期');
  const testsBeforeRefresh = result.requests.filter(item => item.path.endsWith('/llm/test')).length;
  const metadata = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/secrets' && response.request().method() === 'GET');
  await panel.getByRole('button', { name: '刷新观察版本', exact: true }).click();
  assert.equal((await metadata).status(), 200);
  await expect(newStream).toContainText('已过期');
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/test')).length, testsBeforeRefresh);
  await panel.getByRole('button', { name: '测试配置模式', exact: true }).click();
  const latest = panel.locator('.configuration-probe-result').filter({ hasText: '测试配置模式' });
  await expect(latest.getByRole('status')).toContainText('本次通过');
  await expect(latest).toContainText('r2');
  await expect(latest).not.toContainText('已过期');
  result.phase = 'management-receipt-invalidates-observation';
  await form.getByRole('button', { name: '更多操作...', exact: true }).click();
  await page.locator('.monaco-menu-container:visible').getByText('管理 API 密钥', { exact: true }).click();
  await quick.locator('.monaco-list-row').filter({ hasText: '探测共享密钥' }).click();
  await quick.locator('.monaco-list-row').filter({ hasText: '替换密钥值' }).click();
  const replaced = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/secrets/' + key.id && response.request().method() === 'PUT');
  await input('managed-fixture-input');
  assert.equal((await replaced).status(), 200);
  await quick.locator('input').first().press('Escape');
  await expect(latest).toContainText('已过期');
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/test')).length, testsBeforeRefresh + 1);
  assert.equal(result.requests.filter(item => item.method === 'PUT').length, 0);
  result.phase = 'cancel-before-first-content';
  for (const model of ['blocked-1', 'blocked-2', 'blocked-3', 'blocked-4', 'slow-content']) {
    await form.getByRole('button', { name: '添加模型', exact: true }).click();
    await input(model);
    await expect(form.getByRole('button', { name: model, exact: true })).toBeVisible();
  }
  await form.getByRole('button', { name: 'blocked-4', exact: true }).click();
  const connections = async () => (await readFile(fixture.modelEvents, 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
  for (const [mode, ordinal] of [['流式', 1], ['非流式', 2]]) {
    await panel.getByRole('button', { name: '测试' + mode, exact: true }).click();
    await expect.poll(async () => (await connections()).filter(event => event.event === 'opened').length).toBe(ordinal);
    const attempt = panel.locator('.configuration-probe-result[data-probe-model="blocked-4"]').filter({ hasText: '测试' + mode });
    await expect(attempt.getByRole('status')).toContainText('进行中');
    await expect(attempt.locator('pre')).toHaveCount(0);
    await panel.getByRole('button', { name: '停止批量测试', exact: true }).click();
    await expect(attempt.getByRole('status')).toContainText('已取消');
    await expect.poll(async () => (await connections()).filter(event => event.event === 'disconnected').length).toBe(ordinal);
  }
  result.phase = 'batch-stop-releases-three-without-dispatching-fourth';
  for (let index = 1; index <= 4; index++) await panel.getByLabel(`blocked-${index} · blocked-${index}`, { exact: true }).check();
  const beforeBatch = result.requests.filter(item => item.path.endsWith('/llm/test')).length;
  await panel.getByRole('button', { name: '测试选中模型', exact: true }).click();
  await expect.poll(async () => (await connections()).filter(event => event.event === 'opened').length).toBe(5);
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/test')).length, beforeBatch + 3);
  await panel.getByRole('button', { name: '停止批量测试', exact: true }).click();
  await expect.poll(async () => (await connections()).filter(event => event.event === 'disconnected').length).toBe(5);
  for (let index = 1; index <= 3; index++) {
    await expect(panel.locator(`.configuration-probe-result[data-probe-model="blocked-${index}"]`).getByRole('status')).toContainText('已取消');
  }
  await expect(panel.locator('.configuration-probe-result[data-probe-model="blocked-4"]').filter({ hasText: '测试配置模式' })).toHaveCount(0);
  assert.equal(result.requests.filter(item => item.path.endsWith('/llm/test')).length, beforeBatch + 3);
  result.phase = 'slow-first-content-retains-test-deadline';
  await form.getByRole('button', { name: 'slow-content', exact: true }).click();
  const slowResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/test'));
  await panel.getByRole('button', { name: '测试配置模式', exact: true }).click();
  const slow = panel.locator('.configuration-probe-result[data-probe-model="slow-content"]');
  await expect(slow.getByRole('status')).toContainText('进行中');
  await expect(slow.getByRole('status')).toContainText('本次通过', { timeout: 30_000 });
  const slowEvents = (await (await slowResponse).text()).split('\n\n');
  const terminal = JSON.parse(slowEvents.find(event => event.startsWith('event: result\n')).split('data: ')[1]);
  assert(terminal.first_content_ms >= 15_000, '真实首内容等待应超过普通 JSON 的15秒期限');
  assert.equal(terminal.status, 'passed');
  result.slowFirstContentMs = terminal.first_content_ms;
  result.connections = await connections();
  assert.equal(result.requests.filter(item => item.method === 'PUT').length, 0);

  result.phase = 'late-real-directory-response-isolation';
  await panel.getByRole('checkbox', { name: '连接编辑结束后自动获取一次模型', exact: true }).uncheck();
  const discoveredCount = () => result.requests.filter(item => item.path.endsWith('/llm/discover')).length;
  const beforeManual = discoveredCount();
  const lateEndpoint = fixture.endpoint.replace('/v1', '/late/v1');
  const currentEndpoint = fixture.endpoint.replace('/v1', '/current/v1');
  await form.getByLabel('API 地址').first().fill(lateEndpoint);
  await panel.getByRole('button', { name: '刷新观察版本', exact: true }).focus();
  assert.equal(discoveredCount(), beforeManual, '关闭自动获取后编辑连接不发现');
  let allowLate, markReady, markDelivered;
  const releaseLate = new Promise(resolve => { allowLate = resolve; });
  const lateReady = new Promise(resolve => { markReady = resolve; });
  const lateDelivered = new Promise(resolve => { markDelivered = resolve; });
  const delayOldResponse = async route => {
    if (!route.request().postDataJSON().jsonc?.includes(lateEndpoint)) { await route.continue(); return; }
    // 仅延迟真实 Gateway 已返回的目录，不伪造模型数据；实际取消另由上游断连场景验证。
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    markReady();
    await releaseLate;
    await route.fulfill({ response });
    markDelivered();
  };
  await page.route('**/api/v1/config/llm/discover', delayOldResponse);
  try {
    await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
    await lateReady;
    await form.getByLabel('API 地址').first().fill(currentEndpoint);
    await panel.getByRole('button', { name: '刷新观察版本', exact: true }).focus();
    const currentDirectory = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover'));
    await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
    assert.equal((await currentDirectory).status(), 200);
    await panel.getByLabel('搜索上游模型', { exact: true }).fill('');
    await expect(panel.locator('.configuration-probe-models').first()).toContainText('current-only');
    allowLate();
    await lateDelivered;
    await expect(panel.locator('.configuration-probe-models').first()).toContainText('current-only');
    await expect(panel.locator('.configuration-probe-models').first()).not.toContainText('stale-only');
  } finally { allowLate(); await page.unroute('**/api/v1/config/llm/discover', delayOldResponse); }
  assert.equal(discoveredCount(), beforeManual + 2, '显式刷新各发一次，迟到响应不重试');

  result.phase = 'partial-directory-retains-models-and-manual-entry';
  await form.getByLabel('API 地址').first().fill(fixture.endpoint.replace('/v1', '/partial/v1'));
  const partialDirectory = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover'));
  await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
  const partial = (await (await partialDirectory).json()).data;
  assert.equal(partial.complete, false);
  assert.equal(partial.models.length, 0, '单页超过上限时不能把截取的前缀冒充完整页');
  assert.equal(partial.error.category, 'count_limit');
  await expect(panel.getByText('目录不完整：0 个模型，已有模型继续保留', { exact: true })).toBeVisible();
  await expect(form.getByRole('button', { name: 'blocked-4', exact: true })).toBeVisible();
  await form.getByRole('button', { name: '添加模型', exact: true }).click();
  await input('manual-after-partial');
  await expect(form.getByRole('button', { name: 'manual-after-partial', exact: true })).toBeVisible();
  assert.equal(discoveredCount(), beforeManual + 3, '手填模型不重新发现');
  result.directoryBoundary = { delayedRealResponse: true, partialModels: partial.models.length, manualEntryRetained: true };

  await command('boxteam.settings.modelsProviders', location, 'primary', 'default');
  await expect(form.locator('[data-definition-id="primary"] [data-model-id="default"]')).toBeVisible();
  await panel.getByText('测试选项', { exact: true }).click();
  await panel.getByLabel(/^本次操作使用的输入/).selectOption('draft');
  const sourcePath = '/api/v1/config/sources/workspace_mutable_override';
  const sourceExchange = (method, url, body) => page.evaluate(async ({ method, url, body, fixture }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(url, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token,
      'X-BoxTeam-Target-Gateway-Id': fixture.gatewayId, 'X-BoxTeam-Workspace-Id': fixture.workspaceId },
      ...(body ? { body: JSON.stringify(body) } : {}) });
    const envelope = await response.json();
    if (!response.ok || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return envelope.data;
  }, { method, url, body, fixture });
  const saveOtherSource = async (text, label) => {
    const otherSource = await sourceExchange('GET', sourcePath);
    const submission = { gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
      source_key: otherSource.source_key, jsonc: text, base_file_digest: otherSource.file_digest,
      base_layer_revision: otherSource.layer_revision, base_layer_digest: otherSource.layer_digest,
      expected_active_revision: otherSource.active_revision, expected_active_digest: otherSource.active_digest,
      idempotency_key: label + randomUUID() };
    await sourceExchange('POST', sourcePath + '/preflight', submission);
    return sourceExchange('PUT', sourcePath, submission);
  };
  result.phase = 'other-source-active-rejects-old-browser-baseline';
  const activeText = JSON.stringify({ llm: { providers: { primary: { defaults: {
    request_options: { default_headers: { 'X-Active-Source': 'browser-active' } },
  } } } } });
  const savedActive = await saveOtherSource(activeText, 'browser-active-');
  assert.equal(savedActive.edit.source_saved, true);
  assert.equal(savedActive.edit.state, 'active');
  const staleActiveCatalog = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover'));
  await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
  assert.equal((await staleActiveCatalog).status(), 409);
  const staleActiveInput = result.requests.filter(item => item.path.endsWith('/llm/discover')).at(-1).body;
  const staleActiveTest = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/test'));
  await panel.getByRole('button', { name: '测试配置模式', exact: true }).click();
  assert.equal((await staleActiveTest).status(), 409, '发现与推理均须拒绝已过期来源');
  await expect(panel.locator('.configuration-probe-result').getByRole('status')).toContainText('本次失败');
  const beforeActiveRefresh = result.requests.filter(item => item.path.includes('/llm/')).length;
  await panel.getByRole('button', { name: '刷新观察版本', exact: true }).click();
  await expect(panel.getByRole('button', { name: '刷新观察版本', exact: true })).toBeEnabled();
  assert.equal(result.requests.filter(item => item.path.includes('/llm/')).length, beforeActiveRefresh,
    '刷新观察版本不得自动发现或推理');
  const activeCatalog = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover'));
  await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
  const activeDirectoryResponse = await activeCatalog;
  assert.equal(activeDirectoryResponse.status(), 200);
  const activeObservation = (await activeDirectoryResponse.json()).data.observation;
  const activeInput = result.requests.filter(item => item.path.endsWith('/llm/discover')).at(-1).body;
  assert.equal(activeInput.edit_version, staleActiveInput.edit_version, '外部保存及观察刷新不算本页编辑');
  assert.notDeepEqual(activeInput.expected_source_baseline, staleActiveInput.expected_source_baseline);
  const activeTest = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/test'));
  await panel.getByRole('button', { name: '测试配置模式', exact: true }).click();
  const activeEvents = await (await activeTest).text();
  await expect(panel.locator('.configuration-probe-result').getByRole('status')).toContainText('本次通过');
  const activeTerminal = JSON.parse(activeEvents.split('\n\n').find(event => event.startsWith('event: result\n')).split('data: ')[1]);
  assert.deepEqual(activeTerminal.observation.source_baseline, activeObservation.source_baseline);
  result.activeSource = { state: savedActive.edit.state, oldBaselineRejected: true,
    editVersion: activeInput.edit_version, observation: activeTerminal.observation };

  result.phase = 'other-source-pending-rejects-old-browser-baseline';
  const pendingText = JSON.stringify({ llm: { providers: { primary: { defaults: {
    request_options: { default_headers: { 'X-Pending-Source': 'browser-pending' } },
  } } } }, runtime: { workspace: { files: { list: { default_limit: 501 } } } } });
  const savedPending = await saveOtherSource(pendingText, 'browser-pending-');
  assert.equal(savedPending.edit.source_saved, true);
  assert.equal(savedPending.edit.state, 'pending_restart');
  const rejected = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover'));
  await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
  assert.equal((await rejected).status(), 409, '另一来源已保存后，旧浏览器基线必须在联网前拒绝');
  const staleInput = result.requests.filter(item => item.path.endsWith('/llm/discover')).at(-1).body;
  await panel.getByLabel(/^本次操作使用的输入/).selectOption('active');
  await panel.getByRole('button', { name: '测试配置模式', exact: true }).click();
  await expect(panel.locator('.configuration-probe-result').getByRole('status')).toContainText('本次通过');
  await panel.getByRole('button', { name: '刷新观察版本', exact: true }).click();
  await expect(panel.getByRole('button', { name: '刷新观察版本', exact: true })).toBeEnabled();
  await panel.getByLabel(/^本次操作使用的输入/).selectOption('draft');
  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/llm/discover'));
  await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
  assert.equal((await refreshed).status(), 200);
  const currentInput = result.requests.filter(item => item.path.endsWith('/llm/discover')).at(-1).body;
  assert.equal(currentInput.edit_version, staleInput.edit_version, '显式刷新基线不冒充本页编辑');
  assert.notDeepEqual(currentInput.expected_source_baseline, staleInput.expected_source_baseline);
  await panel.getByRole('button', { name: '测试配置模式', exact: true }).click();
  await expect(panel.locator('.configuration-probe-result').getByRole('status')).toContainText('本次通过');
  result.pendingSource = { state: savedPending.edit.state, editVersion: currentInput.edit_version,
    oldBaselineRejected: true, activeSeparate: true };

  result.phase = 'gateway-three-protocols-and-network-before-rejection';
  const probeExchange = body => page.evaluate(async ({ body, fixture }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch('/api/v1/config/llm/test', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Local-Token': token,
        'X-BoxTeam-Target-Gateway-Id': fixture.gatewayId, 'X-BoxTeam-Workspace-Id': fixture.workspaceId },
      body: JSON.stringify(body) });
    return { status: response.status, requestId: response.headers.get('X-Request-ID'), text: await response.text() };
  }, { body, fixture });
  const originalInput = result.requests.filter(item => item.path.endsWith('/llm/test')).at(-1).body;
  result.protocolWires = [];
  for (const protocol of ['chat_completions', 'responses', 'anthropic_messages']) {
    const draft = JSON.parse(originalInput.jsonc), provider = draft.llm.providers.primary;
    provider.defaults.endpoint = protocol === 'anthropic_messages' ? fixture.endpoint.replace(/\/v1$/, '') : fixture.endpoint;
    provider.defaults.custom_llm_provider = protocol === 'anthropic_messages' ? 'anthropic' : 'openai';
    provider.defaults.api_mode = { protocol, streaming: false,
      model_info: { supports_reasoning: false, supports_function_calling: false }, supports_reasoning: {} };
    provider.defaults.request_options.parameters = { temperature: 0.5, top_p: 0.9 };
    provider.models.default.overrides = { request_options: { parameters: {
      temperature: 0, top_p: null, extra_body: { extension_switch: false, extension_value: null },
    } } };
    const input = { ...originalInput, jsonc: JSON.stringify(draft), streaming: false };
    const response = await probeExchange(input);
    assert.equal(response.status, 200, response.text);
    const events = response.text.trim().split('\n\n').map(frame => JSON.parse(frame.split('data: ')[1]));
    assert.equal(events.at(-1).status, 'passed', response.text);
    assert(events.every(event => event.request_id === response.requestId));
    assert.equal(events[0].observation.protocol, protocol);
    assert.deepEqual(events[0].observation, events.at(-1).observation);
    result.protocolWires.push({ protocol, requestId: response.requestId });
    if (protocol === 'chat_completions') {
      for (const parameters of [
        { temperature: 0, extra_body: { temperature: 1 } },
        { max_tokens: 128, max_output_tokens: 129 },
      ]) {
        provider.models.default.overrides.request_options.parameters = parameters;
        const rejected = await probeExchange({ ...input, jsonc: JSON.stringify(draft) });
        assert.equal(rejected.status, 422, rejected.text);
        assert(rejected.requestId);
      }
    }
  }

  result.phase = 'missing-terminal-eof-is-visible-failure';
  // 只截断一次真实 Gateway 回执的终态，验证传输中断；不把它称为真实服务故障。
  const eofRoute = '**/api/v1/config/llm/test';
  await page.route(eofRoute, async route => {
    const response = await route.fetch(), text = await response.text();
    const terminal = text.lastIndexOf('event: result\n');
    assert(terminal > 0 && text.includes('event: delta\n'));
    await route.fulfill({ response, body: text.slice(0, terminal) });
  });
  try {
    const beforeEof = result.requests.filter(item => item.path.endsWith('/llm/test')).length;
    await panel.getByRole('button', { name: '测试流式', exact: true }).click();
    const attempt = panel.locator('.configuration-probe-result').filter({ hasText: '测试流式' });
    await expect(attempt.getByRole('status')).toContainText('本次失败');
    await expect(attempt.getByRole('alert')).toContainText('连接结束但没有终态');
    assert.equal(result.requests.filter(item => item.path.endsWith('/llm/test')).length, beforeEof + 1);
    result.eofBoundary = '真实 Gateway 正文去掉 result，页面失败且零 POST 重放';
  } finally { await page.unroute(eofRoute); }
  assert.deepEqual(result.pageErrors, []);
  result.phase = 'complete';
} catch (error) {
  failures.push(error);
  result.error = error.stack ?? String(error);
} finally {
  // 诊断失败仍须释放自己取得的浏览器，保留全部真实失败。
  const cleanup = [
    ...(page && failures.length ? [() => page.screenshot({ path: path.join(fixture.artifacts, 'failure.png'), fullPage: true })] : []),
    () => writeFile(path.join(fixture.artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n'),
    ...(context ? [() => context.close()] : []), ...(browser ? [() => browser.close()] : []),
  ];
  for (const task of cleanup) { try { await task(); } catch (error) { failures.push(error); } }
}
if (failures.length) throw new AggregateError(failures, `模型发现／测试验收失败: ${result.phase}`);
process.stdout.write(JSON.stringify(result) + '\n');
