import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { parse } from 'jsonc-parser';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_ENTITIES_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], consoleErrors: [], writes: [], saves: [], modelRequests: [], initialization: [] };
let browser, page;
const secret = 'integration-secret-' + randomUUID();
const safe = value => typeof value === 'string' ? value.replaceAll(secret, '[test-secret-redacted]') : JSON.parse(JSON.stringify(value).replaceAll(secret, '[test-secret-redacted]'));
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  page.on('request', request => {
    if (request.method() === 'PUT' && request.url().includes('/config/sources/')) result.writes.push(safe(request.postDataJSON()));
    if (/\/messages(?:\?|$)|\/chat\/completions|\/responses(?:\?|$)/.test(new URL(request.url()).pathname)) result.modelRequests.push(request.url());
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (url, owner, init = {}) => page.evaluate(async ({ url, owner, init }) => {
    const started = performance.now();
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.token, 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId,
      'X-BoxTeam-Workspace-Id': owner.workspaceId } });
    const raw = await response.text(), envelope = JSON.parse(raw);
    if (!response.ok || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return { status: response.status, requestId: envelope.request_id, data: envelope.data,
      bytes: new TextEncoder().encode(raw).byteLength, elapsedMs: performance.now() - started };
  }, { url, owner, init });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push(await api(url, fixture));
  const location = { version: 2, gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
    source_key: 'workspace_root_mutable_override', name: '定义管理工作区' };
  const sourceUrl = '/api/v1/config/sources/' + location.source_key;
  const original = (await api(sourceUrl, fixture)).data;
  const hasProviderOverride = Array.isArray(parse(original.jsonc).llm?.providers);
  const quick = page.locator('.quick-input-widget');
  const modal = page.locator('.monaco-dialog-box[role="dialog"]');
  const form = kind => page.locator(`.configuration-entities[data-definition-kind="${kind}"]`);
  const input = async value => {
    await expect(quick).toBeVisible();
    const control = quick.locator('input').first();
    await expect(control).toBeFocused(); await control.fill(value); await control.press('Enter');
  };
  const save = async kind => {
    await form(kind).getByRole('button', { name: '保存来源', exact: true }).click();
    await expect(modal).toContainText('保存目标配置来源');
    const response = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname === sourceUrl);
    await modal.getByRole('button', { name: '保存来源', exact: true }).click();
    const saved = await response;
    assert.equal(saved.status(), 200, await saved.text());
    const receipt = await saved.json(); assert(receipt.data.edit.source_saved); assert.equal(receipt.request_id, saved.headers()['x-request-id']);
    result.saves.push({ kind, edit: receipt.data.edit, status: receipt.data.status, requestId: receipt.request_id });
    await expect(page.locator('.tabs-container .tab.active')).not.toHaveClass(/\bdirty\b/);
  };
  result.phase = 'provider-create';
  const started = performance.now();
  await command('boxteam.settings.modelsProviders', location);
  await expect(form('providers')).toBeVisible();
  result.firstAvailableMs = performance.now() - started;
  assert(result.firstAvailableMs <= 3000, `定义管理首次可用 ${result.firstAvailableMs}ms`);
  await form('providers').getByRole('button', { name: '新增 Provider', exact: true }).click();
  await input('entities-provider'); await input('entities-model'); await input('https://entities.example/v1'); await input('openai');
  await expect(quick).toBeVisible(); await quick.locator('.monaco-list-row').filter({ hasText: 'chat_completions' }).click();
  await input('${ENTITIES_MISSING_KEY}');
  if (!hasProviderOverride) {
    await expect(modal).toContainText('在本层建立整组 Provider 覆盖');
    await modal.getByRole('button', { name: '新增 Provider', exact: true }).click();
  }
  await expect(form('providers').locator('[data-definition-id="entities-provider"]')).toBeVisible();
  assert.equal(result.writes.length, 0, '编辑表单不能自动保存');
  result.phase = 'same-jsonc-model';
  await form('providers').getByRole('button', { name: '打开来源 JSONC', exact: true }).click();
  await expect(page.locator('.tabs-container .tab.active')).toContainText(location.source_key + '.jsonc');
  await expect(page.locator('.editor-instance:visible .monaco-editor .view-lines').last()).toContainText('entities-provider');
  await command('boxteam.settings.modelsProviders', location, 'entities-provider');
  await expect(form('providers').locator('[data-definition-id="entities-provider"]')).toBeVisible();
  await form('providers').getByLabel('模型名称', { exact: true }).fill('entities-model-edited');
  await save('providers');
  const providerSaved = (await api(sourceUrl, fixture)).data;
  const savedProvider = parse(providerSaved.jsonc).llm.providers.find(provider => provider.id === 'entities-provider');
  assert.equal(savedProvider.model, 'entities-model-edited');
  assert.equal(parse(result.writes.at(-1).jsonc).llm.providers.find(provider => provider.id === 'entities-provider').api_key, '${ENTITIES_MISSING_KEY}');
  assert(savedProvider.api_key.startsWith('boxteam-redacted:'), '公开来源文档继续隐藏凭据输入');
  result.phase = 'secret-cancel';
  const setSecret = () => form('providers').getByRole('button', { name: '设置 API 密钥', exact: true }).click();
  let writeCount = result.writes.length;
  await setSecret();
  await expect(quick.locator('input[type="password"]')).toBeFocused();
  await quick.locator('input[type="password"]').fill(secret);
  await quick.locator('input[type="password"]').press('Escape');
  await expect(quick).not.toBeVisible();
  assert.equal(result.writes.length, writeCount);
  await setSecret();
  await input(secret);
  await expect(modal).toContainText('保存目标配置来源');
  assert(!(await modal.innerText()).includes(secret), '确认不能显示密码');
  await modal.getByRole('button', { name: '取消', exact: true }).click();
  await expect(modal).not.toBeVisible();
  assert.equal(result.writes.length, writeCount, '预检后取消仍零PUT');

  result.phase = 'secret-external-conflict';
  const externalSource = (await api(sourceUrl, fixture)).data;
  const external = { gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId, source_key: location.source_key,
    jsonc: externalSource.jsonc + '\n// 另一个配置编辑者的真实提交\n', base_file_digest: externalSource.file_digest,
    base_layer_revision: externalSource.layer_revision, base_layer_digest: externalSource.layer_digest,
    expected_active_revision: externalSource.active_revision, expected_active_digest: externalSource.active_digest, idempotency_key: randomUUID() };
  await api(sourceUrl + '/preflight', fixture, { method: 'POST', body: JSON.stringify(external) });
  await api(sourceUrl, fixture, { method: 'PUT', body: JSON.stringify(external) });
  writeCount = result.writes.length;
  const conflicted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === sourceUrl + '/preflight' && response.status() === 409);
  await setSecret(); await input(secret); await conflicted;
  await expect(page.locator('.notification-list-item-message').filter({ hasText: '草稿已保留' })).toBeVisible();
  assert.equal(result.writes.length, writeCount, '密码预检冲突不能发PUT');
  // 冲突没有可保留的密码草稿，干净原模型重读后，下一次输入可沿新CAS正常提交。
  result.phase = 'secret-unknown-reload';
  let savedSecret, failedQueries = 0, dropQuery = true;
  const sourceRoute = async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname !== sourceUrl) return route.continue();
    if (request.method() === 'PUT') {
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      const envelope = await response.json();
      savedSecret = envelope.data.edit;
      assert(savedSecret.source_saved);
      assert(!JSON.stringify(envelope).includes(secret), '真实保存回执不能包含密码');
      return route.abort('failed');
    }
    if (request.method() === 'GET' && url.searchParams.has('idempotency_key') && dropQuery) {
      failedQueries++;
      return route.abort('failed');
    }
    return route.continue();
  };
  await page.route('**/api/v1/config/sources/**', sourceRoute);
  await setSecret(); await input(secret);
  await expect(modal).toContainText('保存目标配置来源');
  await modal.getByRole('button', { name: '保存来源', exact: true }).click();
  await expect.poll(() => failedQueries).toBeGreaterThan(0);
  await expect(form('providers').getByRole('button', { name: '查询原提交', exact: true })).toBeVisible();
  assert(savedSecret?.idempotency_key);
  assert.equal(result.writes.length, writeCount + 1);
  // 提交身份在PUT前已持久保存；重载时只让真实原键查询通过，不重放写入。
  dropQuery = false;
  const queryAfterReload = page.waitForResponse(response => response.request().method() === 'GET'
    && new URL(response.url()).pathname === sourceUrl
    && new URL(response.url()).searchParams.get('idempotency_key') === savedSecret.idempotency_key);
  await page.reload({ waitUntil: 'domcontentloaded' });
  const reconciled = await queryAfterReload, envelope = await reconciled.json();
  assert.equal(reconciled.status(), 200);
  assert.equal(envelope.request_id, reconciled.headers()['x-request-id']);
  assert.equal(envelope.data.edit.idempotency_key, savedSecret.idempotency_key);
  assert.equal(envelope.data.edit.source_saved, true);
  assert.equal(envelope.data.edit.file_digest, envelope.data.file_digest);
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  await command('boxteam.settings.modelsProviders', location, 'entities-provider');
  await expect(form('providers').getByRole('button', { name: '设置 API 密钥', exact: true })).toBeEnabled();
  assert.equal(result.writes.length, writeCount + 1, '重开只查询，不重放密码PUT');
  assert(!(await page.locator('body').innerText()).includes(secret));
  await page.unroute('**/api/v1/config/sources/**', sourceRoute);
  // 打开同一来源正文，再撤销；密码不能被放入原文本模型或撤销历史。
  await form('providers').getByRole('button', { name: '打开来源 JSONC', exact: true }).click();
  const sourceText = page.locator('.editor-instance:visible .monaco-editor .view-lines').last();
  await expect(sourceText).toContainText('entities-provider');
  assert(!(await sourceText.innerText()).includes(secret));
  await command('undo');
  assert(!(await sourceText.innerText()).includes(secret));
  await command('boxteam.settings.modelsProviders', location, 'entities-provider');
  // 原Workbench存储与热退出备份只允许非秘密查询身份，不存密码正文。
  await command('boxteam.flushViewState');
  const leaked = await page.evaluate(async needle => {
    if (Object.values(localStorage).some(value => value.includes(needle))) return true;
    for (const { name } of await indexedDB.databases()) {
      if (!name) continue;
      const database = await new Promise((resolve, reject) => { const request = indexedDB.open(name); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
      try {
        for (const storeName of database.objectStoreNames) {
          const transaction = database.transaction(storeName, 'readonly');
          const values = await new Promise((resolve, reject) => { const request = transaction.objectStore(storeName).getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
          for (const value of values) {
            const text = value instanceof ArrayBuffer ? new TextDecoder().decode(value)
              : ArrayBuffer.isView(value) ? new TextDecoder().decode(value) : JSON.stringify(value);
            if (text?.includes(needle)) return true;
          }
        }
      } finally { database.close(); }
    }
    return false;
  }, secret);
  assert.equal(leaked, false, '本机工作台存储不能包含密码');
  result.secret = { cancelledBeforeInputSubmit: true, cancelledAfterPreflight: true, externalConflictRecovered: true,
    unknownRecoveredByOriginalKey: savedSecret.idempotency_key, puts: result.writes.length - writeCount, requestId: envelope.request_id };
  result.phase = 'agent-create';
  await page.locator('.settings-context-label').getByRole('link', { name: 'Agent 管理', exact: true }).click();
  await expect(form('agents')).toBeVisible();
  await form('agents').getByRole('button', { name: '新增 Agent', exact: true }).click();
  await input('entities-agent'); await input('定义管理 Agent'); await input('此提示词来自真实管理表单');
  await expect(quick).toBeVisible(); await quick.locator('.monaco-list-row').filter({ hasText: 'entities-provider' }).click();
  await expect(form('agents').locator('[data-definition-id="entities-agent"]')).toBeVisible();
  await form('agents').getByLabel('显示名', { exact: true }).fill('修改后的显示名');
  await save('agents');
  const created = (await api(sourceUrl, fixture)).data;
  assert.equal(parse(created.jsonc).agents['entities-agent'].name, '修改后的显示名');
  assert.equal(created.source_effective.agents['entities-agent'].instructions.system_prompt, '此提示词来自真实管理表单');
  assert.equal(created.source_effective.agents['entities-agent'].model.primary_provider, 'entities-provider');
  // 来源保存与运行应用分开断言，不能把待重启的候选冒充当前生效值。
  if (created.status.restart_required) {
    assert.equal(created.effective.agents['entities-agent'], undefined);
    await expect(form('agents')).toContainText('当前生效配置中不存在');
    await expect(page.locator('.settings-context-label')).toContainText('需要重启');
  } else {
    assert.equal(created.effective.agents['entities-agent'].model.primary_provider, 'entities-provider');
  }
  result.phase = 'agent-reload-remove';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`)).toBeVisible({ timeout: 30000 });
  await command('boxteam.settings.agents', location, 'entities-agent');
  await expect(form('agents').getByLabel('显示名', { exact: true })).toHaveValue('修改后的显示名');
  await form('agents').getByRole('button', { name: '移除本层定义', exact: true }).click();
  await expect(modal).toContainText('entities-agent');
  await modal.getByRole('button', { name: '移除本层定义', exact: true }).click();
  await save('agents');
  const removed = (await api(sourceUrl, fixture)).data;
  assert.equal(removed.source_effective.agents['entities-agent'], undefined);
  result.phase = 'session-selection-links';
  const session = (await api('/api/v1/sessions', fixture, { method: 'POST', body: JSON.stringify({ title: '定义管理返回会话', agent_id: 'default' }) })).data;
  const mainThread = (await api(`/api/v1/sessions/${session.session_id}/main-thread`, fixture)).data;
  await page.getByTestId(`catalog-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}-${session.session_id}`)
    .locator(`button[data-session-id="${session.session_id}"]`).dblclick();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${fixture.workspaceId}"][data-session-id="${session.session_id}"][data-thread-id="${mainThread.thread_id}"]`);
  await expect(thread.locator('.composer-model-pill')).toBeVisible({ timeout: 30000 });
  await thread.locator('.composer-model-pill').click();
  const manage = thread.getByRole('menuitem', { name: '模型与 Provider', exact: true });
  await manage.click();
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: location.source_key }).click();
  await expect(form('providers')).toBeVisible();
  await form('providers').getByRole('button', { name: '前往会话选择 Agent／模型', exact: true }).click();
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: '定义管理返回会话' }).click();
  await expect(thread.locator('.composer-model-pill')).toBeFocused();
  await thread.locator('.composer-model-pill').press('Escape');
  await thread.locator('.composer-agent-button').click();
  await thread.getByRole('menuitem', { name: '管理 Agent 定义', exact: true }).click();
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: location.source_key }).click();
  await expect(form('agents')).toBeVisible();
  await form('agents').getByRole('button', { name: '前往会话选择 Agent／模型', exact: true }).click();
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: '定义管理返回会话' }).click();
  await expect(thread.locator('.composer-agent-button')).toBeFocused();
  result.sessionSelection = { sessionId: session.session_id, threadId: mainThread.thread_id };
  result.phase = 'workspace-session-defaults';
  await command('boxteam.settings.agents', location, 'coder');
  await expect(form('agents')).toBeVisible();
  const beforeDefaults = (await api(sourceUrl, fixture)).data;
  const beforeSession = (await api('/api/v1/sessions/' + session.session_id, fixture)).data;
  const activeAgents = (await api('/api/v1/agents', fixture)).data;
  const coder = activeAgents.find(agent => agent.agent_id === 'coder');
  assert(coder && !coder.workspace_default, 'fixture必须提供与原默认值不同的 coder');
  const providerId = coder.providers[0].provider_id;
  // 来源 default_agent 保留为未保存草稿；会话默认值仍使用当前 active 定义和独立 owner。
  await form('agents').getByRole('button', { name: '设为本来源的配置默认 Agent', exact: true }).click();
  await expect(page.locator('.tabs-container .tab.active')).toHaveClass(/\bdirty\b/);
  const sourceWritesBeforeDefaults = result.writes.length;
  const defaultsWrites = [];
  page.on('request', request => {
    if (request.method() === 'PUT' && /\/api\/v1\/agents\/(?:workspace-default|[^/]+\/workspace-default-provider)$/.test(new URL(request.url()).pathname)) {
      const headers = request.headers();
      defaultsWrites.push({ path: new URL(request.url()).pathname, body: request.postDataJSON(), headers: {
        'x-boxteam-target-gateway-id': headers['x-boxteam-target-gateway-id'],
        'x-boxteam-workspace-id': headers['x-boxteam-workspace-id'],
      } });
    }
  });
  const openDefaults = async () => {
    await form('agents').getByRole('button', { name: '工作区新会话默认值', exact: true }).click();
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: 'coder' }).click();
    await expect(quick).toBeVisible();
  };
  await openDefaults();
  await quick.locator('.monaco-list-row').filter({ hasText: '设为新会话默认 Agent' }).click();
  await expect(modal).toContainText(fixture.workspaceId);
  await modal.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(defaultsWrites.length, 0, '取消默认值确认必须零PUT');
  await openDefaults();
  await quick.locator('.monaco-list-row').filter({ hasText: '设为新会话默认 Agent' }).click();
  const agentDefaultResponse = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === '/api/v1/agents/workspace-default');
  await modal.getByRole('button', { name: '保存新会话默认值', exact: true }).click();
  const changedAgent = await agentDefaultResponse, changedAgentEnvelope = await changedAgent.json();
  assert.equal(changedAgent.status(), 200);
  assert.equal(changedAgentEnvelope.request_id, changedAgent.headers()['x-request-id']);
  assert.equal(changedAgentEnvelope.data.length, activeAgents.length);
  assert.equal(changedAgentEnvelope.data.find(agent => agent.workspace_default).agent_id, 'coder');
  await openDefaults();
  await quick.locator('.monaco-list-row').filter({ hasText: `设为 ${coder.agent_id} 的新会话默认 Provider` }).click();
  const providerDefaultResponse = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === '/api/v1/agents/coder/workspace-default-provider');
  await modal.getByRole('button', { name: '保存新会话默认值', exact: true }).click();
  const changedProvider = await providerDefaultResponse, changedProviderEnvelope = await changedProvider.json();
  assert.equal(changedProvider.status(), 200);
  assert.equal(changedProviderEnvelope.request_id, changedProvider.headers()['x-request-id']);
  assert.equal(changedProviderEnvelope.data.length, activeAgents.length);
  assert.equal(changedProviderEnvelope.data.find(agent => agent.agent_id === 'coder').providers.find(provider => provider.workspace_default).provider_id, providerId);
  assert.equal(defaultsWrites.length, 2);
  assert.deepEqual(defaultsWrites.map(write => write.body), [
    { agent_id: coder.agent_id }, { provider_id: providerId },
  ]);
  assert(defaultsWrites.every(write => write.headers['x-boxteam-target-gateway-id'] === fixture.gatewayId
    && write.headers['x-boxteam-workspace-id'] === fixture.workspaceId));
  assert.equal(result.writes.length, sourceWritesBeforeDefaults, 'SessionDefaults不能提交未保存的来源');
  const afterDefaults = (await api(sourceUrl, fixture)).data;
  assert.equal(afterDefaults.file_digest, beforeDefaults.file_digest);
  assert.equal(afterDefaults.jsonc, beforeDefaults.jsonc);
  await expect(page.locator('.tabs-container .tab.active')).toHaveClass(/\bdirty\b/);
  const preserved = (await api('/api/v1/sessions/' + session.session_id, fixture)).data;
  assert.equal(preserved.current_agent_id, beforeSession.current_agent_id);
  assert.equal(preserved.current_provider_id, beforeSession.current_provider_id);
  const nextSession = (await api('/api/v1/sessions', fixture, { method: 'POST', body: JSON.stringify({ title: '采用新默认值的会话' }) })).data;
  assert.equal(nextSession.current_agent_id, 'coder');
  assert.equal(nextSession.current_provider_id, providerId);
  result.sessionDefaults = { cancelled: true, writes: defaultsWrites, agentReceipt: changedAgentEnvelope,
    providerReceipt: changedProviderEnvelope, originalSessionId: session.session_id, newSessionId: nextSession.session_id };
  // 本切片只采一个实际复用的列表 API；保存来源详情仍沿已有领域证据。
  const samples = [];
  for (let index = 0; index < 21; index++) {
    const sample = await api('/api/v1/agents', fixture);
    samples.push({ elapsedMs: sample.elapsedMs, bytes: sample.bytes, requestId: sample.requestId, count: sample.data.length });
  }
  const warm = samples.slice(1).map(sample => sample.elapsedMs).sort((a, b) => a - b);
  result.defaultsMeasurements = { population: activeAgents.length, first: samples[0], samples,
    p50: warm[Math.ceil(warm.length * .50) - 1], p95: warm[Math.ceil(warm.length * .95) - 1], max: warm.at(-1) };
  assert(result.defaultsMeasurements.p95 <= 500, 'Agent列表暖p95超过500ms');
  assert(result.defaultsMeasurements.max <= 1500, 'Agent列表暖最大值超过1500ms');

  result.phase = 'seed-lower-layer';
  const lowerKey = 'workspace_mutable_override';
  const lowerUrl = '/api/v1/config/sources/' + lowerKey;
  const lowerBefore = (await api(lowerUrl, fixture)).data;
  const lowerJsonc = parse(lowerBefore.jsonc);
  const lowerProvider = { id: 'mcp-plugin-replay', model: 'inherited-model', endpoint: 'https://inherited.example/v1',
    api_key: '${INHERITED_MISSING_KEY}', custom_llm_provider: 'openai',
    api_mode: { protocol: 'chat_completions', model_info: { supports_function_calling: true, supports_reasoning: false },
      supports_reasoning: { reasoning_content: false } } };
  lowerJsonc.llm ??= {};
  lowerJsonc.llm.providers = [...(lowerJsonc.llm.providers ?? []).filter(provider => provider.id !== lowerProvider.id), lowerProvider];
  lowerJsonc.agents ??= {};
  lowerJsonc.agents['inherited-agent'] = { name: '下层 Agent', instructions: { system_prompt: '提示词来自下层来源' },
    model: { primary_provider: lowerProvider.id } };
  const lowerIntent = { gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId, source_key: lowerKey,
    jsonc: JSON.stringify(lowerJsonc, null, 2), base_file_digest: lowerBefore.file_digest,
    base_layer_revision: lowerBefore.layer_revision, base_layer_digest: lowerBefore.layer_digest,
    expected_active_revision: lowerBefore.active_revision, expected_active_digest: lowerBefore.active_digest, idempotency_key: randomUUID() };
  await api(lowerUrl + '/preflight', fixture, { method: 'POST', body: JSON.stringify(lowerIntent) });
  await api(lowerUrl, fixture, { method: 'PUT', body: JSON.stringify(lowerIntent) });
  const writesBeforeInheritance = result.writes.length;
  result.seededSourceWrite = true;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`)).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  await command('boxteam.settings.agents', location, 'inherited-agent');
  await expect(form('agents').getByLabel('显示名', { exact: true })).toHaveValue('下层 Agent');
  await form('agents').getByRole('button', { name: '新增 Agent', exact: true }).click();
  await input('inherited-agent');
  await input('本层覆盖 Agent');
  await input('提示词来自本层覆盖');
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: lowerProvider.id }).click();
  await expect(modal).toContainText('覆盖继承的 Agent inherited-agent');
  await modal.getByRole('button', { name: '新增 Agent', exact: true }).click();
  await expect(form('agents').locator('[data-definition-id="inherited-agent"]')).toBeVisible();
  await save('agents');
  const agentOverride = (await api(sourceUrl, fixture)).data;
  assert.equal(parse(agentOverride.jsonc).agents['inherited-agent'].name, '本层覆盖 Agent');
  assert.equal(agentOverride.source_effective.agents['inherited-agent'].instructions.system_prompt, '提示词来自本层覆盖');
  await form('agents').getByRole('button', { name: '移除本层定义', exact: true }).click();
  await expect(modal).toContainText('inherited-agent');
  await modal.getByRole('button', { name: '移除本层定义', exact: true }).click();
  await save('agents');
  const agentRestored = (await api(sourceUrl, fixture)).data;
  assert.equal(parse(agentRestored.jsonc).agents?.['inherited-agent'], undefined);
  assert.equal(agentRestored.source_effective.agents['inherited-agent'].name, '下层 Agent');
  assert.equal(agentRestored.source_effective.agents['inherited-agent'].instructions.system_prompt, '提示词来自下层来源');

  result.phase = 'provider-remove-and-restore-inheritance';
  await page.locator('.settings-context-label').getByRole('link', { name: '模型与 Provider', exact: true }).click();
  await form('providers').getByRole('button').filter({ hasText: 'entities-provider' }).click();
  await form('providers').getByRole('button', { name: '移除本层定义', exact: true }).click();
  await expect(modal).toContainText('entities-provider');
  await modal.getByRole('button', { name: '移除本层定义', exact: true }).click();
  await save('providers');
  const providerRemoved = (await api(sourceUrl, fixture)).data;
  assert(!providerRemoved.source_effective.llm.providers.some(provider => provider.id === 'entities-provider'));
  await form('providers').getByRole('button', { name: '恢复整组 Provider 继承', exact: true }).click();
  await expect(modal).toContainText('恢复整组 Provider 继承');
  await modal.getByRole('button', { name: '恢复整组 Provider 继承', exact: true }).click();
  await save('providers');
  const providerRestored = (await api(sourceUrl, fixture)).data;
  const lowerAfterRestore = (await api(lowerUrl, fixture)).data;
  assert.equal(parse(providerRestored.jsonc).llm?.providers, undefined, '当前层 Provider 数组覆盖必须整体移除');
  assert.deepEqual(providerRestored.source_effective.llm.providers, lowerAfterRestore.source_effective.llm.providers);
  assert.equal(providerRestored.source_effective.llm.providers.find(provider => provider.id === lowerProvider.id)?.model, lowerProvider.model);
  assert.equal(result.writes.length, writesBeforeInheritance + 4, '仅Agent覆盖/删除及Provider删除/整组恢复写入本层');
  result.inheritance = { agentId: 'inherited-agent', providerId: lowerProvider.id, lowerSource: lowerKey };
  assert.equal(result.modelRequests.length, 0);
  assert(result.writes.every(write => write.gateway_id === fixture.gatewayId && write.workspace_id === fixture.workspaceId));
  assert.deepEqual(result.pageErrors, []);
  await page.screenshot({ path: path.join(artifacts, 'agent-definitions.png') });
  result.phase = 'completed';
} catch (error) {
  result.error = safe(String(error?.stack ?? error)); process.exitCode = 1;
  if (page) { await page.screenshot({ path: path.join(artifacts, 'failure.png') }); result.body = safe(await page.locator('body').innerText()); }
} finally {
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  await browser?.close();
}
