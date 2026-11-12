import assert from 'node:assert/strict';
import { installPlugin } from '../../../support/plugin_package.mjs';
import { writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { parse, modify, applyEdits } from 'jsonc-parser';

const input = JSON.parse(process.env.BOXTEAM_SETTINGS_SOURCE_FIXTURE);
const result = { phase: 'initialization', pageErrors: [], writes: [], preflights: [], sourceReads: [], responses: [], sourceDiscoveries: [] };
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.setDefaultTimeout(15_000);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  result.consoleErrors = [];
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/config/sources')) result.sourceDiscoveries.push({ phase: result.phase, path: url.pathname,
      method: request.method(), gateway: request.headers()['x-boxteam-target-gateway-id'], workspace: request.headers()['x-boxteam-workspace-id'] });
    if (!url.pathname.includes('/config/sources/')) return;
    const body = request.postData() ? JSON.parse(request.postData()) : undefined;
    const record = { path: url.pathname, query: url.search, gateway_id: body?.gateway_id, workspace_id: body?.workspace_id,
      idempotency_key: body?.idempotency_key, jsonc_bytes: body?.jsonc?.length };
    if (request.method() === 'PUT') result.writes.push(record);
    else if (request.method() === 'POST') result.preflights.push(record);
    else result.sourceReads.push(record);
  });
  page.on('response', response => { if (new URL(response.url()).pathname.includes('/config/sources/')) result.responses.push({ path: new URL(response.url()).pathname, status: response.status() }); });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
  const api = (path, options = {}) => page.evaluate(async ({ path, options }) => {
    const auth = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(path, { ...options, headers: { 'X-Local-Token': auth.token, 'Content-Type': 'application/json', ...(options.headers ?? {}) },
      body: options.body && JSON.stringify(options.body) });
    const envelope = await response.json();
    if (!response.ok || envelope.code !== 0 || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(`${path}: ${response.status} ${JSON.stringify(envelope)}`);
    return envelope.data;
  }, { path, options });
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  const health = await api('/api/gateway/health');
  const workspaces = await api('/api/gateway/workspaces');
  await api('/api/v1/workspace');
  const active = workspaces.active_workspace;
  assert(active?.workspace_id && active.gateway_id === health.gateway_id);
  const workspace = workspaces.items.find(item => item.workspace_id === active.workspace_id && item.gateway_id === health.gateway_id);
  const gatewayHeaders = { 'X-BoxTeam-Target-Gateway-Id': health.gateway_id };
  const workspaceHeaders = { ...gatewayHeaders, 'X-BoxTeam-Workspace-Id': active.workspace_id };
  result.owner = { gatewayId: health.gateway_id, workspaceId: active.workspace_id };
  const quick = page.locator('.quick-input-widget');
  const modal = page.locator('.monaco-dialog-box[role="dialog"]');
  const text = page.locator('.editor-instance:visible .monaco-editor .view-lines').last();
  const tab = page.locator('.tabs-container .tab.active:visible').last();
  const pick = async label => {
    await expect(quick).toBeVisible();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click();
  };
  const open = async (target, source) => {
    const opened = command('boxteam.settings.openSource');
    await pick(target); await pick(source); await opened;
    await expect(text).toBeVisible();
    await expect(tab).toContainText(source + '.jsonc');
  };
  const replace = async content => {
    await text.click(); await page.keyboard.press('Control+Home'); await page.keyboard.press('Control+A');
    await page.evaluate(value => navigator.clipboard.writeText(value), content);
    await page.keyboard.press('Control+V');
    await expect(tab).toHaveClass(/\bdirty\b/);
  };
  const save = async () => {
    const saved = command('workbench.action.files.save');
    await expect(modal).toContainText('保存目标配置来源', { timeout: 30_000 });
    await modal.getByRole('button', { name: '保存来源', exact: true }).click();
    await saved;
    await expect(tab).not.toHaveClass(/\bdirty\b/);
  };
  const gatewayPath = '/api/gateway/config/sources/gateway_mutable_override';
  const workspacePath = '/api/v1/config/sources/workspace_root_mutable_override';
  const update = (jsonc, path, value) => applyEdits(jsonc, modify(jsonc, path, value, { formattingOptions: { insertSpaces: true, tabSize: 2 } }));
  const resourceSource = (source, gatewayScope) => quick.locator('.monaco-list-row').filter({ hasText: source + '.jsonc' })
    .filter({ hasText: gatewayScope ? '配置来源 · Gateway ·' : `配置来源 · ${workspace.name} ·` });
  const showSources = async (source = 'workspace_root_mutable_override', gatewayScope = false) => {
    await command('boxteam.quickAccess.resources', result.owner);
    await expect(quick.locator('input').first()).toHaveValue('resource ');
    await quick.locator('input').first().fill('resource ' + source);
    await expect(resourceSource(source, gatewayScope)).toBeVisible();
  };
  result.phase = 'resource-discovery';
  const readsBeforeDiscovery = result.sourceReads.length;
  await showSources();
  assert.equal(result.sourceDiscoveries.filter(item => item.phase === result.phase).length, 2);
  assert.equal(result.sourceReads.length, readsBeforeDiscovery, '浏览配置来源不能提前读正文');
  assert.equal(result.writes.length, 0);
  await quick.locator('input').first().fill('resource gateway_mutable_override');
  await expect(resourceSource('gateway_mutable_override', true)).toBeVisible();
  assert.equal(result.sourceDiscoveries.filter(item => item.phase === result.phase).length, 2, '逐键筛选不能重读目录');
  await page.keyboard.press('Escape');
  assert.equal(result.sourceReads.length, readsBeforeDiscovery);
  result.resource_browse_only = true;
  result.phase = 'gateway-save';
  await showSources('gateway_mutable_override', true); await resourceSource('gateway_mutable_override', true).click();
  await expect(tab).toContainText('gateway_mutable_override.jsonc');
  let gatewayDocument = await api(gatewayPath, { headers: gatewayHeaders });
  const gatewayDraft = '// Gateway 来源注释\n' + update(gatewayDocument.jsonc, ['logging', 'runtime', 'level'], 'debug');
  await replace(gatewayDraft);
  // 保留 Web 默认 autoSave；来源编辑必须自行只允许显式保存。
  await page.waitForTimeout(1500);
  assert.equal(result.writes.length, 0);
  await save();
  gatewayDocument = await api(gatewayPath, { headers: gatewayHeaders });
  assert.equal(parse(gatewayDocument.jsonc).logging.runtime.level, 'debug');
  assert(gatewayDocument.jsonc.includes('// Gateway 来源注释'));
  assert(gatewayDocument.edit === null || gatewayDocument.edit === undefined);
  result.gateway_saved = true;

  result.phase = 'workspace-save-with-frozen-owner';
  await showSources(); await resourceSource('workspace_root_mutable_override', false).click({ modifiers: ['Alt'] });
  await expect(page.locator('.editor-group-container')).toHaveCount(2);
  await expect(tab).toContainText('workspace_root_mutable_override.jsonc');
  result.resource_open_and_save = true;
  let workspaceDocument = await api(workspacePath, { headers: workspaceHeaders });
  const workspaceDraft = '// Workspace 来源注释\n' + update(workspaceDocument.jsonc, ['default_agent'], 'default');
  await replace(workspaceDraft);
  // 焦点切换到 Gateway 设置再返回，原工作区文件的目标不随管理选择变化。
  await command('workbench.action.previousEditor');
  await command('workbench.action.nextEditor');
  await save();
  workspaceDocument = await api(workspacePath, { headers: workspaceHeaders });
  assert.equal(parse(workspaceDocument.jsonc).default_agent, 'default');
  assert(workspaceDocument.jsonc.includes('// Workspace 来源注释'));
  const workspaceWrite = result.writes.find(record => record.path === workspacePath);
  assert.equal(workspaceWrite.workspace_id, active.workspace_id);
  result.workspace_saved = true; result.frozen_owner = true;

  result.phase = 'real-cas-conflict';
  const draft = workspaceDocument.jsonc + '\n// 冲突时必须保留的草稿\n';
  await replace(draft);
  // 并发修改也经正式 source 入口，不能由测试预写文件或数据库制造保存成功。
  const external = { gateway_id: health.gateway_id, workspace_id: active.workspace_id, source_key: workspaceDocument.source_key,
    jsonc: workspaceDocument.jsonc + '\n// 另一编辑者已提交\n', base_file_digest: workspaceDocument.file_digest,
    base_layer_revision: workspaceDocument.layer_revision, base_layer_digest: workspaceDocument.layer_digest,
    expected_active_revision: workspaceDocument.active_revision, expected_active_digest: workspaceDocument.active_digest, idempotency_key: randomUUID() };
  await api(workspacePath + '/preflight', { method: 'POST', headers: workspaceHeaders, body: external });
  await api(workspacePath, { method: 'PUT', headers: workspaceHeaders, body: external });
  // 原热退出备份须带上编辑时的 CAS；刷新不能把旧草稿换成最新来源基线。
  page.once('dialog', dialog => dialog.accept());
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  await expect(text).toContainText('冲突时必须保留的草稿');
  await expect(tab).toHaveClass(/\bdirty\b/);
  result.draft_restored_after_reload = true;
  const count = result.writes.length;
  const rejectedPreflight = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === workspacePath + '/preflight' && response.status() === 409);
  await command('workbench.action.files.save');
  await rejectedPreflight;
  await expect(page.locator('.notification-list-item-message').filter({ hasText: '草稿已保留' })).toBeVisible();
  assert.equal(result.writes.length, count, '预检冲突不得发起 PUT');
  await expect(text).toContainText('冲突时必须保留的草稿');
  await expect(tab).toHaveClass(/\bdirty\b/);
  assert((await api(workspacePath, { headers: workspaceHeaders })).jsonc.includes('另一编辑者已提交'));
  result.conflict_kept_draft = true;
  const rebased = command('boxteam.settings.rebaseSource');
  await expect(modal).toContainText('以最新来源重新预检');
  await modal.getByRole('button', { name: '使用最新基线', exact: true }).click(); await rebased;
  await expect(text).toContainText('冲突时必须保留的草稿'); await save();

  result.phase = 'committed-response-lost';
  await replace(draft + '// 本次提交响应丢失\n');
  let intercepted = 0;
  const dropSavedResponse = async route => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    intercepted++;
    // 真正的 owner 已完成提交后丢弃响应；这是显式传输故障替身。
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    await route.abort('connectionreset');
  };
  await page.route('**/api/v1/config/sources/workspace_root_mutable_override', dropSavedResponse);
  const beforeUnknown = result.writes.length;
  await save();
  await page.unroute('**/api/v1/config/sources/workspace_root_mutable_override', dropSavedResponse);
  assert.equal(intercepted, 1); assert.equal(result.writes.length, beforeUnknown + 1);
  const originalKey = result.writes.at(-1).idempotency_key;
  assert(result.sourceReads.some(record => new URLSearchParams(record.query).get('idempotency_key') === originalKey));
  assert((await api(workspacePath, { headers: workspaceHeaders })).jsonc.includes('本次提交响应丢失'));
  result.unknown_not_replayed = true;
  result.transportFault = '真实 PUT 提交成功后由 Playwright 丢弃响应；原键 GET 确认，不重发 PUT';

  result.phase = 'manifest-settings-without-runtime';
  await installPlugin(api, { gateway_id: health.gateway_id, path: input.packagePath });
  const beforeBinding = await api(gatewayPath, { headers: gatewayHeaders });
  const binding = { package_id: 'source.settings-probe', package_version: '1.0.0', plugin_id: 'preference', enabled: false };
  const bindingIntent = { gateway_id: health.gateway_id, source_key: beforeBinding.source_key,
    jsonc: update(beforeBinding.jsonc, ['plugins', 'runtime', 'entries'], [binding]), base_file_digest: beforeBinding.file_digest,
    base_layer_revision: beforeBinding.layer_revision, base_layer_digest: beforeBinding.layer_digest,
    expected_active_revision: beforeBinding.active_revision, expected_active_digest: beforeBinding.active_digest, idempotency_key: randomUUID() };
  await api(gatewayPath + '/preflight', { method: 'POST', headers: gatewayHeaders, body: bindingIntent });
  await api(gatewayPath, { method: 'PUT', headers: gatewayHeaders, body: bindingIntent });
  // 原 JSON 标签仍持有旧 CAS；先沿正式 revert 读回新来源，不能由测试给模型注入基线。
  await open('Gateway 配置', 'gateway_mutable_override');
  await command('workbench.action.files.revert');

  result.phase = 'original-settings-editor';
  const oldHostRequests = [];
  const hostRequest = request => { if (new URL(request.url()).pathname.includes('/extension-configurations/')) oldHostRequests.push(request.url()); };
  page.on('request', hostRequest);
  const openingForm = command('boxteam.settings.openTarget');
  await pick('Gateway 配置'); await pick('gateway_mutable_override'); await openingForm;
  const settings = page.locator('.settings-editor:visible').last();
  await expect(settings).toBeVisible();
  await expect(settings.locator('.settings-context-label')).toContainText(health.gateway_id);
  await command('settings.action.search');
  await page.keyboard.insertText('@id:logging.runtime.pretty');
  const pretty = settings.locator('[data-key="logging.runtime.pretty"]');
  await expect(pretty).toBeVisible();
  const expectedPretty = !parse((await api(gatewayPath, { headers: gatewayHeaders })).jsonc).logging.runtime.pretty;
  const beforeFormWrite = result.writes.length;
  await pretty.getByRole('checkbox').click();
  await expect(tab).toHaveClass(/\bdirty\b/);
  // 表单先改原文本草稿；打开 JSON、回到表单仍共享同一 dirty 模型，未自动提交。
  await command('settings.switchToJSON');
  await expect(tab).toContainText('gateway_mutable_override');
  await expect(tab).toHaveClass(/\bdirty\b/);
  await text.click(); await page.keyboard.press('Control+A'); await page.keyboard.press('Control+C');
  const sharedDraft = await page.evaluate(() => navigator.clipboard.readText());
  assert.equal(parse(sharedDraft).logging.runtime.pretty, expectedPretty);
  const returningForm = command('boxteam.settings.openTarget');
  await pick('Gateway 配置'); await pick('gateway_mutable_override'); await returningForm;
  await expect(settings).toBeVisible();
  assert.equal(result.writes.length, beforeFormWrite);
  result.form_and_json_share_draft = true;
  const formSaved = command('workbench.action.files.save');
  await expect(modal).toContainText('保存目标配置来源');
  await modal.getByRole('button', { name: '保存来源', exact: true }).click();
  await formSaved;
  await expect.poll(async () => parse((await api(gatewayPath, { headers: gatewayHeaders })).jsonc).logging.runtime.pretty).toBe(expectedPretty);
  await command('settings.action.search');
  await page.keyboard.press('Control+A'); await page.keyboard.insertText('@modified @id:logging.runtime.pretty');
  await expect(pretty).toBeVisible();
  await command('settings.action.search');
  await page.keyboard.press('Control+A'); await page.keyboard.insertText('@ext:source.settings-probe/preference');
  const parameter = settings.locator('[data-key="plugins.configuration.values.source%2Esettings-probe.preference.visible"]');
  await expect(parameter).toBeVisible();
  await parameter.getByRole('checkbox').click();
  await expect(tab).toHaveClass(/\bdirty\b/);
  const parameterSaved = command('workbench.action.files.save');
  await expect(modal).toContainText('保存目标配置来源');
  await modal.getByRole('button', { name: '保存来源', exact: true }).click(); await parameterSaved;
  const pluginDocument = await api(gatewayPath, { headers: gatewayHeaders });
  assert.equal(parse(pluginDocument.jsonc).plugins.configuration.values['source.settings-probe'].preference.visible, true);
  const runtime = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: health.gateway_id } });
  assert(runtime.plugins.filter(plugin => plugin.binding.package_id === binding.package_id).every(plugin => !plugin.instance));
  result.plugin_schema_saved_without_runtime = true;
  await command('settings.switchToJSON');
  await expect(text).toBeVisible();
  await expect(tab).toContainText('gateway_mutable_override');
  assert.deepEqual(oldHostRequests, [], '正式来源设置不能调用旧 profile/view 配置桥');
  page.off('request', hostRequest);
  result.original_settings_form_saved = true;

  result.phase = 'readonly-and-state';
  const sources = await api('/api/gateway/config/sources', { headers: gatewayHeaders });
  const inline = sources.sources.find(source => source.layer === 'inline' && source.source_key);
  assert(inline);
  await open('Gateway 配置', inline.source_key);
  const readonlyBefore = result.writes.length;
  await text.click(); await page.keyboard.press('Control+Home'); await page.keyboard.insertText('// 不得写入\n');
  await expect(tab).not.toHaveClass(/\bdirty\b/);
  assert.equal(result.writes.length, readonlyBefore);
  await expect(text).not.toContainText('不得写入');
  await showSources('inline'); await resourceSource('inline', false).click();
  await expect(tab).toContainText('inline.jsonc');
  await text.click(); await page.keyboard.press('Control+Home'); await page.keyboard.insertText('// 工作区内置来源不得写入\n');
  await expect(tab).not.toHaveClass(/\bdirty\b/);
  await expect(text).not.toContainText('工作区内置来源不得写入');
  assert.equal(result.writes.length, readonlyBefore);
  result.readonly = true;
  result.resource_readonly = true;
  result.phase = 'configuration-resource-uri';
  const openConfigurationUri = async (uri, sourceKey, modifiers = []) => {
    const parsed = await api('/api/gateway/resource-addresses/parse', { method: 'POST',
      body: { uri, local_gateway_id: health.gateway_id } });
    await command('workbench.action.quickOpen', parsed.reference_uri);
    const link = quick.locator(`[data-quick-input-id="link:${parsed.reference_uri}"]`);
    await expect(link).toBeVisible();
    await link.click({ modifiers });
    await expect(tab).toContainText(sourceKey + '.jsonc');
    await expect(text).toBeVisible();
    return parsed;
  };
  assert(inline.uri, 'Gateway inline 来源必须由原 owner 提供 URI');
  await openConfigurationUri(inline.uri, inline.source_key);
  const workspaceSources = await api('/api/v1/config/sources', { headers: workspaceHeaders });
  const workspaceInline = workspaceSources.sources.find(source => source.layer === 'inline' && source.source_key && source.uri);
  assert(workspaceInline, 'Workspace inline 来源必须由原 owner 提供 URI');
  await openConfigurationUri(workspaceInline.uri, workspaceInline.source_key, ['Alt']);
  await text.click(); await page.keyboard.press('Control+Home'); await page.keyboard.insertText('// URI 打开仍只读\n');
  await expect(tab).not.toHaveClass(/\bdirty\b/);
  await expect(text).not.toContainText('URI 打开仍只读');
  assert.equal(result.writes.length, readonlyBefore, '配置 URI 打开不得绕过原来源 writer');
  result.configuration_uri_opened = true;
  const status = command('boxteam.settings.sourceStatus');
  await expect(quick).toBeVisible(); await expect(quick).toContainText('来源层');
  await page.keyboard.press('Escape'); await status;
  assert(!result.sourceReads.some(record => /\/(gateway_schema|workspace_schema)$/.test(record.path)), 'schema 必须复用 owner 文档，不能请求虚构来源');
  assert.deepEqual(result.pageErrors, []);
  result.phase = 'complete';
} catch (error) {
  result.error = error.stack ?? String(error);
  if (page) await page.screenshot({ path: `${input.artifacts}/settings-source-editor-failure.png`, fullPage: true });
  throw error;
} finally {
  await writeFile(input.resultPath, JSON.stringify(result, null, 2) + '\n');
  await browser?.close();
}
