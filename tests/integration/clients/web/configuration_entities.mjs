import assert from 'node:assert/strict';
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
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  page.on('request', request => {
    if (request.method() === 'PUT' && request.url().includes('/config/sources/')) result.writes.push(request.postDataJSON());
    if (/\/messages(?:\?|$)|\/chat\/completions|\/responses(?:\?|$)/.test(new URL(request.url()).pathname)) result.modelRequests.push(request.url());
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = (url, owner, init = {}) => page.evaluate(async ({ url, owner, init }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': credential.token, 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId,
      'X-BoxTeam-Workspace-Id': owner.workspaceId } });
    const envelope = await response.json();
    if (!response.ok || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return { status: response.status, requestId: envelope.request_id, data: envelope.data };
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
  await expect(page.locator('.settings-context-label')).toBeVisible({ timeout: 30000 });
  await command('boxteam.settings.agents', location, 'entities-agent');
  await expect(form('agents').getByLabel('显示名', { exact: true })).toHaveValue('修改后的显示名');
  await form('agents').getByRole('button', { name: '移除本层定义', exact: true }).click();
  await expect(modal).toContainText('entities-agent');
  await modal.getByRole('button', { name: '移除本层定义', exact: true }).click();
  await save('agents');
  const removed = (await api(sourceUrl, fixture)).data;
  assert.equal(removed.source_effective.agents['entities-agent'], undefined);
  result.phase = 'provider-remove';
  await page.locator('.settings-context-label').getByRole('link', { name: '模型与 Provider', exact: true }).click();
  await form('providers').getByRole('button').filter({ hasText: 'entities-provider' }).click();
  await form('providers').getByRole('button', { name: '移除本层定义', exact: true }).click();
  await expect(modal).toContainText('entities-provider');
  await modal.getByRole('button', { name: '移除本层定义', exact: true }).click();
  await save('providers');
  const providerRemoved = (await api(sourceUrl, fixture)).data;
  assert(!providerRemoved.source_effective.llm.providers.some(provider => provider.id === 'entities-provider'));
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
  assert.equal(result.modelRequests.length, 0);
  assert(result.writes.every(write => write.gateway_id === fixture.gatewayId && write.workspace_id === fixture.workspaceId));
  assert.deepEqual(result.pageErrors, []);
  await page.screenshot({ path: path.join(artifacts, 'agent-definitions.png') });
  result.phase = 'completed';
} catch (error) {
  result.error = String(error?.stack ?? error); process.exitCode = 1;
  if (page) { await page.screenshot({ path: path.join(artifacts, 'failure.png') }); result.body = await page.locator('body').innerText(); }
} finally {
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  await browser?.close();
}
