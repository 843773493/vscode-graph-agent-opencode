import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { parse } from 'jsonc-parser';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { webSelectors } from '../../../clients/selectors/web-selectors.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_LIVE_FIXTURE);
const shellOnly = fixture.scenario === 'shell-environment';
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须显式设置正式测试产物目录');
const result = { phase: 'initialization', scenario: fixture.scenario, pageErrors: [], initialization: [], probes: [], sessions: [], receipts: [] };
let browser, context, page, failure;
const cleanup = [];
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  cleanup.push(() => browser.close());
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  cleanup.push(() => context.close());
  page = await context.newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (url, method = 'GET', body) => page.evaluate(async ({ url, method, body, owner }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.token,
      'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const envelope = await response.json();
    if (!response.ok || !envelope.request_id || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return { data: envelope.data, requestId: envelope.request_id };
  }, { url, method, body, owner: fixture });
  const openDetails = async locator => { if (!(await locator.evaluate(element => element.open))) await locator.locator(':scope > summary').click(); };
  const receipt = async response => {
    assert.equal(response.status(), 200, await response.text());
    const body = await response.json();
    assert.equal(body.request_id, response.headers()['x-request-id']);
    result.receipts.push({ path: new URL(response.url()).pathname, requestId: body.request_id, observedAt: Date.now() });
    return body.data;
  };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push({ url, ...await api(url) });
  assert.equal(result.initialization[0].data.gateway_id, fixture.gatewayId);
  assert.equal(result.initialization[1].data.active_workspace.workspace_id, fixture.workspaceId);
  assert.equal(result.initialization[2].data.workspace_id, fixture.workspaceId);
  const terminalSession = (await api('/api/v1/sessions', 'POST', { title: '真实PTY代理验收', agent_id: 'default' })).data;
  const terminalMain = (await api(`/api/v1/sessions/${terminalSession.session_id}/main-thread`)).data;
  await command('_workbench.open', `boxteam://${fixture.gatewayId}/workspace/${fixture.workspaceId}/resources/session/${terminalSession.session_id}/threads/${terminalMain.thread_id}`);
  await expect(page.locator(`section.boxteam-thread[data-session-id="${terminalSession.session_id}"]`).getByTestId('composer-input')).toBeVisible({ timeout: 30000 });
  result.phase = 'real-pty-inherited-proxy';
  await command('boxteam.terminal.focus');
  const terminalPanel = page.locator(`.native-workspace-panel[data-panel-kind="terminal"][data-workspace-id="${fixture.workspaceId}"]`).getByTestId('terminal-panel');
  await expect(terminalPanel).toBeVisible();
  const created = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/resources/terminals');
  await page.getByRole('button', { name: '新建终端', exact: true }).and(page.locator('.codicon-add')).click();
  const terminal = await receipt(await created);
  assert.equal(terminal.workspace_id, fixture.workspaceId);
  const frame = terminalPanel.frameLocator('.terminal-panel-frame');
  // Workbench 隐藏 iframe 内工具栏；真实 attach 状态与后续 PTY 输出共同证明可用。
  await expect(frame.locator('#attach-toggle')).toHaveClass(/\bis-attached\b/, { timeout: 30000 });
  const input = frame.locator('.xterm-helper-textarea');
  const terminalDigest = async (targetFrame, marker) => {
    const targetInput = targetFrame.locator('.xterm-helper-textarea');
    const script = `import {createHash} from "node:crypto"; const names=${JSON.stringify(['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'])}; const digest=createHash("sha256").update(JSON.stringify(names.map(name=>[name,process.env[name]??null]))).digest("hex"); console.log(["BOXTEAM","ENV",${JSON.stringify(marker)},digest].join("_"));`;
    await targetInput.focus();
    await targetInput.pressSequentially(`node --input-type=module -e '${script}'`, { delay: 1 });
    await targetInput.press('Enter');
    const expected = new RegExp(`BOXTEAM_ENV_${marker}_([a-f0-9]{64})`);
    await expect(targetFrame.locator('.xterm-rows')).toContainText(expected);
    return expected.exec(await targetFrame.locator('.xterm-rows').innerText())[1];
  };
  const baselineEnvironmentDigest = await terminalDigest(frame, 'BASELINE');
  assert.equal(baselineEnvironmentDigest, fixture.environmentDigest, '首个PTY必须继承原启动环境，不能用全部空值冒充');
  if (!shellOnly) {
  await input.focus();
  // 没有 export 或 --proxy；分段 marker 排除命令回显造成的假通过。
  const shell = "curl --fail --silent --show-error --head --max-time 40 https://example.com >/dev/null; code=$?; printf 'BOXTEAM_%s_%s_%s\\n' 'PTY' 'HTTPS' \"$code\"";
  const ptyStartedAt = Date.now();
  await input.pressSequentially(shell, { delay: 1 });
  await input.press('Enter');
  await expect(frame.locator('.xterm-rows')).toContainText('BOXTEAM_PTY_HTTPS_0', { timeout: 50000 });
  result.terminal = { resourceId: terminal.resource_id, startedAt: ptyStartedAt, completedAt: Date.now(),
    command: shell, output: await frame.locator('.xterm-rows').innerText() };
  const location = { version: 2, gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
    source_key: 'workspace_root_mutable_override', name: '真实代理模型验收' };
  const form = page.locator('.configuration-entities[data-definition-kind="providers"]');
  const article = form.locator('article[data-definition-id]');
  result.phase = 'ui-create-provider';
  await command('boxteam.settings.modelsProviders', location, 'primary');
  await page.getByRole('button', { name: '添加模型服务', exact: true }).click();
  await expect(article).not.toHaveAttribute('data-definition-id', 'primary');
  const providerId = await article.getAttribute('data-definition-id');
  assert(providerId.startsWith('provider-'));
  result.providerId = providerId;
  const panel = article.getByRole('region', { name: '发现与测试模型', exact: true });
  await openDetails(panel.locator(':scope > details').first());
  await panel.getByRole('checkbox', { name: '连接编辑结束后自动获取一次模型', exact: true }).uncheck();
  await article.getByRole('button', { name: '从 LiteLLM 目录选择服务商，或手动填写', exact: true }).click();
  await page.getByRole('combobox', { name: '搜索服务商或模型…', exact: true }).fill('自定义服务商');
  await page.locator('.configuration-catalog-popup').getByRole('option', { name: '自定义服务商／兼容接口', exact: true }).click();
  await article.getByRole('textbox', { name: /^API 地址/ }).fill(fixture.endpoint);
  await openDetails(article.locator('.configuration-provider-connection'));
  await article.getByRole('textbox', { name: /^服务适配器/ }).fill('openai');
  await article.getByRole('combobox', { name: /^协议/ }).selectOption('chat_completions');
  await openDetails(article.locator('.configuration-provider-connection .configuration-node-reference'));
  await article.locator('.configuration-provider-connection .configuration-proxy-fields')
    .getByRole('combobox', { name: '代理策略', exact: true }).selectOption('environment');
  await article.locator('.configuration-credential').getByRole('button', { name: '设置 API 密钥', exact: true }).click();
  await page.locator('.quick-input-widget').getByText('使用环境变量', { exact: true }).click();
  const quickInput = page.locator('.quick-input-widget input').first();
  await quickInput.fill(fixture.secretName);
  await quickInput.press('Enter');
  await expect(page.locator('.quick-input-widget')).not.toBeVisible();
  result.phase = 'real-model-discovery';
  const discoveryStartedAt = Date.now();
  const discovered = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/config/llm/discover', { timeout: 90000 });
  await panel.getByRole('button', { name: '获取上游模型', exact: true }).click();
  const discovery = await receipt(await discovered);
  result.discovery = { ...discovery,
    startedAt: discoveryStartedAt, completedAt: Date.now() };
  assert(discovery.complete && !discovery.error, `真实模型目录未完整返回：${JSON.stringify(discovery.error)}`);
  const preferred = discovery.models.find(model => model.model === fixture.model);
  const supported = discovery.models.filter(model => model.metadata?.supports_function_calling === true)
    .sort((left, right) => Number(!/nano|mini|flash|free/i.test(left.model)) - Number(!/nano|mini|flash|free/i.test(right.model)) || left.model.localeCompare(right.model));
  const chosen = preferred ?? supported[0];
  assert(chosen, '实际目录没有预设模型或明确声明支持工具的候选；保留目录事实');
  const upstreamModel = chosen.model;
  result.selection = { requested: fixture.model, selected: upstreamModel, preferredReturned: Boolean(preferred),
    reason: preferred ? '实际目录包含既有测试模型' : '从真实目录选择明确支持工具的候选，流式能力由后续真实测试证明' };
  const candidates = panel.locator('.configuration-upstream-candidates');
  await candidates.getByRole('combobox').selectOption('all');
  await candidates.getByLabel('搜索上游模型', { exact: true }).fill(upstreamModel);
  const candidate = candidates.locator('.configuration-probe-models > label').filter({ has: page.getByText(upstreamModel, { exact: true }) });
  await expect(candidate).toHaveCount(1);
  await candidate.getByRole('checkbox').check();
  await candidates.getByRole('button', { name: '添加选中的模型', exact: true }).click();
  await article.getByRole('navigation', { name: '模型列表', exact: true }).getByRole('button', { name: new RegExp(upstreamModel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) }).click();
  const model = article.locator('[data-model-id]');
  await expect(model).toHaveCount(1);
  const modelId = await model.getAttribute('data-model-id');
  result.modelId = modelId;
  result.phase = 'real-model-probes';
  await openDetails(panel.locator('.configuration-probe-more'));
  for (const label of ['测试流式', '测试非流式']) {
    const startedAt = Date.now();
    const observed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/config/llm/test', { timeout: 90000 });
    await panel.getByRole('button', { name: label, exact: true }).click();
    const response = await observed;
    assert.equal(response.status(), 200);
    const observation = panel.locator('.configuration-probe-result').filter({ hasText: label });
    await expect(observation.getByRole('status')).toContainText('本次通过', { timeout: 75000 });
    assert((await observation.locator('pre').innerText()).trim().length > 0);
    result.probes.push({ label, requestId: response.headers()['x-request-id'], startedAt, completedAt: Date.now(),
      streaming: response.request().postDataJSON().streaming, text: await observation.locator('pre').innerText() });
  }
  const save = async () => {
    await command('notifications.hideToasts');
    await form.getByRole('button', { name: '保存来源', exact: true }).click();
    const modal = page.locator('.monaco-dialog-box[role="dialog"]');
    await expect(modal).toContainText('保存目标配置来源');
    const saved = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname.endsWith('/config/sources/workspace_root_mutable_override'));
    await modal.getByRole('button', { name: '保存来源', exact: true }).click();
    const reply = await receipt(await saved);
    assert(reply.edit.source_saved);
    await expect(page.locator('.tabs-container .tab.active')).not.toHaveClass(/\bdirty\b/);
  };
  const modelRef = { provider_id: providerId, model_id: modelId };
  for (const streaming of [true, false]) {
    result.phase = `formal-session-${streaming ? 'stream' : 'complete'}`;
    await openDetails(model.locator('.configuration-model-overrides'));
    await model.getByRole('combobox', { name: /^响应方式/ }).selectOption(String(streaming));
    await save();
    const inspection = await api(`/api/v1/config/llm/inspection?provider_id=${providerId}&model_id=${modelId}`);
    assert.equal(inspection.data.model_options.options.api_mode.streaming, streaming);
    const session = (await api('/api/v1/sessions', 'POST', { title: `真实代理${streaming ? '流式' : '非流式'}`, agent_id: 'default' })).data;
    await api(`/api/v1/sessions/${session.session_id}`, 'PATCH', { model: modelRef });
    const main = (await api(`/api/v1/sessions/${session.session_id}/main-thread`)).data;
    await command('_workbench.open', `boxteam://${fixture.gatewayId}/workspace/${fixture.workspaceId}/resources/session/${session.session_id}/threads/${main.thread_id}`);
    const thread = page.locator(`section.boxteam-thread[data-workspace-id="${fixture.workspaceId}"][data-session-id="${session.session_id}"][data-thread-id="${main.thread_id}"]`);
    const composer = thread.locator(webSelectors.composer);
    await expect(composer).toBeVisible({ timeout: 30000 });
    const sent = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/v1/sessions/${session.session_id}/threads/${main.thread_id}/messages`);
    await composer.fill('请只回复 BOXTEAM_LIVE_OK，不调用任何工具。');
    const startedAt = Date.now();
    await thread.locator(webSelectors.sendButton).click();
    const accepted = await receipt(await sent);
    const deadline = Date.now() + 90000;
    let job;
    do {
      job = (await api(`/api/v1/jobs/${accepted.job_id}`)).data;
      assert(!['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status), JSON.stringify(job));
      if (job.status === 'completed') break;
      await new Promise(resolve => setTimeout(resolve, 200));
    } while (Date.now() < deadline);
    assert.equal(job.status, 'completed', '真实正式会话须完成业务Job');
    await expect(thread.locator(`.chat-turn[data-turn-id="${accepted.turn_id}"]`)).toContainText('BOXTEAM_LIVE_OK', { timeout: 15000 });
    result.sessions.push({ streaming, startedAt, completedAt: Date.now(), sessionId: session.session_id, threadId: main.thread_id,
      jobId: accepted.job_id, turnId: accepted.turn_id, status: job.status, inspectionRequestId: inspection.requestId });
    if (streaming) await command('boxteam.settings.modelsProviders', location, providerId, modelId);
  }
  result.phase = 'isolated-proxy-failure';
  await command('boxteam.settings.modelsProviders', location, providerId, modelId);
  await openDetails(article.locator('.configuration-provider-connection'));
  await openDetails(article.locator('.configuration-provider-connection .configuration-node-reference'));
  const proxyFields = article.locator('.configuration-provider-connection .configuration-proxy-fields');
  await proxyFields.getByRole('combobox', { name: '代理策略', exact: true }).selectOption('explicit');
  await proxyFields.locator('[data-proxy-field="url"]').fill(fixture.failureProxy);
  await save();
  await openDetails(panel.locator('.configuration-probe-more'));
  const failedAt = Date.now();
  const rejected = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/config/llm/test', { timeout: 75000 });
  await panel.getByRole('button', { name: '测试流式', exact: true }).click();
  const rejectedResponse = await rejected;
  const rejectedObservation = panel.locator('.configuration-probe-result').filter({ hasText: '测试流式' });
  await expect(rejectedObservation.getByRole('status')).toContainText('本次失败', { timeout: 65000 });
  await expect(rejectedObservation.getByRole('alert')).toBeVisible();
  const failureRequest = rejectedResponse.request().postDataJSON();
  assert.equal(failureRequest.timeout_seconds, 60, '沿用产品原模型探测期限');
  result.proxyFailure = { startedAt: failedAt, completedAt: Date.now(), originalDeadlineSeconds: failureRequest.timeout_seconds,
    requestId: rejectedResponse.headers()['x-request-id'], error: await rejectedObservation.getByRole('alert').innerText(),
    internalHealth: await api('/api/gateway/health'), internalWorkspace: await api('/api/v1/workspace') };
  assert.equal(result.proxyFailure.internalWorkspace.data.workspace_id, fixture.workspaceId);
  }
  result.phase = 'workspace-jsonc-does-not-rewrite-shell-environment';
  const sourceUrl = '/api/v1/config/sources/workspace_root_mutable_override';
  const loadedSource = (await api(sourceUrl)).data;
  const workspaceDocument = parse(loadedSource.jsonc);
  const previousWorkspaceProxy = workspaceDocument.settings.network.proxy;
  workspaceDocument.settings.network.proxy = { mode: 'direct' };
  const savedWorkspace = await api(sourceUrl, 'PUT', { gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
    source_key: 'workspace_root_mutable_override', jsonc: JSON.stringify(workspaceDocument, null, 2),
    base_file_digest: loadedSource.file_digest, base_layer_revision: loadedSource.layer_revision,
    base_layer_digest: loadedSource.layer_digest, expected_active_revision: loadedSource.active_revision,
    expected_active_digest: loadedSource.active_digest, idempotency_key: crypto.randomUUID() });
  assert(savedWorkspace.data.edit.source_saved && savedWorkspace.data.edit.state === 'active');
  await command('boxteam.terminal.focus');
  const oldEnvironmentDigest = await terminalDigest(frame, 'OLD');
  const newTerminalResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/resources/terminals');
  await page.getByRole('button', { name: '新建终端', exact: true }).and(page.locator('.codicon-add')).click();
  const secondTerminal = await receipt(await newTerminalResponse);
  assert.notEqual(secondTerminal.resource_id, terminal.resource_id);
  await expect(frame.locator('#attach-toggle')).toHaveClass(/\bis-attached\b/);
  const newEnvironmentDigest = await terminalDigest(frame, 'NEW');
  assert.equal(oldEnvironmentDigest, baselineEnvironmentDigest, 'Workspace JSONC不得重写既有Shell环境');
  assert.equal(newEnvironmentDigest, baselineEnvironmentDigest, 'Workspace JSONC不得改写后续Shell启动环境');
  result.shellEnvironment = { baselineDigest: baselineEnvironmentDigest, oldDigest: oldEnvironmentDigest,
    newDigest: newEnvironmentDigest, previousWorkspaceProxy, savedWorkspaceProxy: workspaceDocument.settings.network.proxy,
    requestId: savedWorkspace.requestId, originalTerminalId: terminal.resource_id, newTerminalId: secondTerminal.resource_id,
    boundary: '只打印六种标准代理变量的SHA256，不打印变量值；未改变插件自有外部HTTP支持范围' };
  if (!shellOnly) assert.deepEqual(result.probes.map(item => item.streaming), [true, false]);
  result.phase = 'completed';
  await page.screenshot({ path: path.join(artifacts, 'live-completed.png'), fullPage: true });
} catch (error) {
  failure = error;
  result.error = { message: error.message, stack: error.stack };
  if (page) await page.screenshot({ path: path.join(artifacts, 'live-failure.png'), fullPage: true }).catch(screenshotError => { result.screenshotError = screenshotError.message; });
} finally {
  result.cleanupErrors = [];
  for (const close of cleanup.reverse()) await close().catch(error => result.cleanupErrors.push(error.message));
  await writeFile(path.join(artifacts, 'live-result.json'), JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
assert.deepEqual(result.pageErrors, []);
assert.deepEqual(result.cleanupErrors, []);
