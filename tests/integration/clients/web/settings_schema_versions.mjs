import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { parse, modify, applyEdits } from 'jsonc-parser';
import { installPlugin } from '../../../support/plugin_package.mjs';

const input = JSON.parse(process.env.BOXTEAM_SETTINGS_SCHEMA_FIXTURE);
const result = { phase: 'initialization', pageErrors: [], requests: [], writes: [], oldHostRequests: [], schemaRequests: [] };
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1800, height: 1100 } });
  page.setDefaultTimeout(15_000);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/service-plugins/configuration-schemas')) result.schemaRequests.push(request.postDataJSON());
    if (path.includes('/extension-configurations/')) result.oldHostRequests.push(path);
    if (request.method() === 'PUT' && path.includes('/config/sources/')) {
      const body = request.postDataJSON();
      result.writes.push({ path, gateway_id: body.gateway_id, workspace_id: body.workspace_id,
        idempotency_key: body.idempotency_key });
    }
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const api = async (path, options = {}, expectedStatus = 200) => {
    const reply = await page.evaluate(async ({ path, options }) => {
      const auth = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
      const response = await fetch(path, { ...options, headers: { 'X-Local-Token': auth.token, 'Content-Type': 'application/json', ...(options.headers ?? {}) },
        body: options.body && JSON.stringify(options.body) });
      return { status: response.status, requestId: response.headers.get('X-Request-ID'), body: await response.json() };
    }, { path, options });
    result.requests.push({ path, method: options.method ?? 'GET', ...reply });
    assert.equal(reply.status, expectedStatus, JSON.stringify(reply));
    assert.equal(reply.body.request_id, reply.requestId);
    if (expectedStatus === 200) { assert.equal(reply.body.code, 0); return reply.body.data; }
    return reply;
  };
  const update = (jsonc, path, value) => applyEdits(jsonc, modify(jsonc, path, value,
    { formattingOptions: { insertSpaces: true, tabSize: 2 } }));
  const binding = version => ({ package_id: 'schema.probe', plugin_id: 'preference', package_version: version, enabled: false });
  const sourcePath = '/api/v1/config/sources/workspace_root_mutable_override';
  await page.goto(input.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  const health = await api('/api/gateway/health');
  await api('/api/gateway/workspaces'); await api('/api/v1/workspace');
  const gatewayHeaders = { 'X-BoxTeam-Target-Gateway-Id': health.gateway_id };
  const headers = workspace => ({ ...gatewayHeaders, 'X-BoxTeam-Workspace-Id': workspace.workspace_id });
  const targets = [];
  result.phase = 'register-and-bind';
  for (const [index, rootPath] of input.workspacePaths.entries()) {
    const name = index === 0 ? 'Schema Workspace A' : 'Schema Workspace B';
    const directory = await api('/api/gateway/managed-workspaces', { method: 'POST', body: { root_path: rootPath, name, create_directory: false } });
    const target = directory.items.find(item => item.name === name);
    assert(target && target.gateway_id === health.gateway_id); targets.push(target);
    await installPlugin(api, { gateway_id: health.gateway_id, path: input.packagePaths[index] });
  }
  for (const [index, target] of targets.entries()) {
    const document = await api(sourcePath, { headers: headers(target) });
    const intent = { gateway_id: health.gateway_id, workspace_id: target.workspace_id, source_key: document.source_key,
      jsonc: update(document.jsonc, ['plugins', 'runtime', 'entries'], [binding(index === 0 ? '1.0.0' : '2.0.0')]),
      base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
      expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: randomUUID() };
    await api(sourcePath + '/preflight', { method: 'POST', headers: headers(target), body: intent });
    await api(sourcePath, { method: 'PUT', headers: headers(target), body: intent });
  }
  const quick = page.locator('.quick-input-widget'), modal = page.locator('.monaco-dialog-box[role="dialog"]');
  const pick = async label => { await expect(quick).toBeVisible(); await quick.locator('.monaco-list-row').filter({ hasText: label }).first().click(); };
  const pane = target => page.locator('.editor-group-container').filter({
    has: page.locator('.settings-context-label').filter({ hasText: target.workspace_id })
  });
  const open = async (target, groupIndex = 1) => {
    await command(groupIndex === 1 ? 'workbench.action.focusFirstEditorGroup' : 'workbench.action.focusSecondEditorGroup');
    const opening = command('boxteam.settings.openTarget');
    await pick(target.name); await pick('workspace_root_mutable_override'); await opening;
    await expect(pane(target)).toHaveCount(1);
    await expect(page.locator('.editor-group-container.active .settings-context-label')).toContainText(target.workspace_id);
    await expect(pane(target).getByRole('tab', { name: new RegExp(target.name) })).toHaveCount(1);
  };
  const form = target => pane(target).locator('.settings-editor:visible');
  const field = (target, name) => form(target).locator(`[data-key="plugins.configuration.values.schema%2Eprobe.preference.${name}"]`);
  const search = async (target, groupIndex, value) => {
    await command(groupIndex === 1 ? 'workbench.action.focusFirstEditorGroup' : 'workbench.action.focusSecondEditorGroup');
    await expect(pane(target)).toHaveCount(1);
    await expect(page.locator('.editor-group-container.active .settings-context-label')).toContainText(target.workspace_id);
    await command('settings.action.search'); await page.keyboard.press('Control+A'); await page.keyboard.insertText(value);
  };
  const save = async () => {
    const saving = command('workbench.action.files.save');
    await expect(modal).toContainText('保存目标配置来源');
    await modal.getByRole('button', { name: '保存来源', exact: true }).click(); await saving;
    await expect(page.locator('.editor-group-container.active .tabs-container .tab.active')).not.toHaveClass(/\bdirty\b/);
  };
  const [a, b] = targets;
  result.targets = targets.map(target => ({ gateway_id: target.gateway_id, workspace_id: target.workspace_id }));
  result.phase = 'two-visible-schema-versions';
  await open(a, 1); await search(a, 1, '@ext:schema.probe/preference');
  await expect(field(a, 'mode').getByRole('textbox')).toHaveValue('first');
  await expect(field(a, 'onlyOne')).toBeVisible();
  await field(a, 'mode').getByRole('textbox').fill('saved-A'); await save();
  const aSaved = await api(sourcePath, { headers: headers(a) });
  await command('workbench.action.splitEditorRight');
  await open(b, 1); await search(b, 1, '@ext:schema.probe/preference');
  await expect(field(b, 'mode').getByRole('checkbox')).toBeChecked();
  await expect(field(b, 'onlyTwo')).toBeVisible();
  await expect(field(b, 'onlyOne')).toHaveCount(0);
  await search(a, 2, '@ext:schema.probe/preference');
  await expect(field(a, 'mode').getByRole('textbox')).toHaveValue('saved-A');
  await expect(field(a, 'onlyOne')).toBeVisible();
  await expect(field(a, 'onlyTwo')).toHaveCount(0);
  await search(b, 1, '@ext:schema.probe/preference');
  await field(b, 'mode').getByRole('checkbox').click(); await save();
  const bSaved = await api(sourcePath, { headers: headers(b) });
  assert.equal(parse(bSaved.jsonc).plugins.configuration.values['schema.probe'].preference.mode, false);
  assert.equal((await api(sourcePath, { headers: headers(a) })).jsonc, aSaved.jsonc);
  result.isolated_versions = true;

  result.phase = 'install-new-version-with-live-form';
  // 保留 B 原表单，让另组的正式 JSONC 保存驱动同一文本模型；不能重开表单替产品补读声明。
  await command('workbench.action.splitEditorRight');
  await search(b, 2, '@ext:schema.probe/preference');
  await command('workbench.action.focusFirstEditorGroup');
  await command('settings.switchToJSON');
  await expect(field(b, 'onlyTwo')).toBeVisible();
  await installPlugin(api, { gateway_id: health.gateway_id, path: input.packagePaths[2] });
  const beforeLateBinding = await api(sourcePath, { headers: headers(b) });
  let lateBinding = update(beforeLateBinding.jsonc, ['plugins', 'runtime', 'entries'], [binding('3.0.0')]);
  lateBinding = update(lateBinding, ['plugins', 'configuration', 'values', 'schema.probe', 'preference'], { mode: 'late-B' });
  const sourceText = page.locator('.editor-group-container.active .monaco-editor .view-lines').last();
  await sourceText.click(); await page.keyboard.press('Control+A');
  await page.evaluate(value => navigator.clipboard.writeText(value), lateBinding); await page.keyboard.press('Control+V');
  const schemaReadsBeforeSave = result.schemaRequests.length;
  await save();
  await expect(field(b, 'onlyThree')).toBeVisible();
  await expect(field(b, 'mode').getByRole('textbox')).toHaveValue('late-B');
  await expect(field(b, 'onlyTwo')).toHaveCount(0);
  assert.equal(result.schemaRequests.length, schemaReadsBeforeSave + 1, '原 B 表单只补读一次新绑定版本');
  result.late_installed_schema_refreshed = true;
  await command('workbench.action.focusSecondEditorGroup');
  await field(b, 'mode').getByRole('textbox').fill('late-B-updated'); await save();
  assert.equal(result.schemaRequests.length, schemaReadsBeforeSave + 1, '普通参数保存不能重新读取安装声明');
  result.ordinary_save_zero_schema_requests = true;
  await command('workbench.action.focusFirstEditorGroup');
  await command('workbench.action.closeEditorsAndGroup');
  await expect(pane(a)).toHaveCount(1); await expect(pane(b)).toHaveCount(1);

  const editBinding = async (target, version) => {
    await command('settings.switchToJSON');
    const text = page.locator('.editor-group-container.active .monaco-editor .view-lines').last();
    await expect(text).toBeVisible();
    const document = await api(sourcePath, { headers: headers(target) });
    let jsonc = update(document.jsonc, ['plugins', 'runtime', 'entries'], version ? [binding(version)] : []);
    if (version) jsonc = update(jsonc, ['plugins', 'configuration', 'values', 'schema.probe', 'preference'], { mode: 'saved-B' });
    await text.click(); await page.keyboard.press('Control+A');
    await page.evaluate(value => navigator.clipboard.writeText(value), jsonc); await page.keyboard.press('Control+V');
    await save(); await open(target, 1); await search(target, 1, '@ext:schema.probe/preference');
  };
  result.phase = 'same-input-version-switch';
  await editBinding(b, '1.0.0');
  await expect(field(b, 'mode').getByRole('textbox')).toHaveValue('saved-B');
  await expect(field(b, 'onlyOne')).toBeVisible(); await expect(field(b, 'onlyTwo')).toHaveCount(0);
  await search(a, 2, '@ext:schema.probe/preference');
  await expect(field(a, 'mode').getByRole('textbox')).toHaveValue('saved-A');
  await search(b, 1, '@ext:schema.probe/preference');
  result.same_input_refreshed = true;
  result.phase = 'unbind-and-uninstall';
  await editBinding(b, undefined);
  await expect(field(b, 'mode')).toHaveCount(0);
  await search(a, 2, '@ext:schema.probe/preference');
  await expect(field(a, 'mode').getByRole('textbox')).toHaveValue('saved-A');
  const residue = await api(sourcePath, { headers: headers(b) });
  assert.equal(parse(residue.jsonc).plugins.configuration.values['schema.probe'].preference.mode, 'saved-B');
  result.unbind_released_only_owner = true; result.residue_preserved = true;
  await api('/api/gateway/service-plugins/uninstall', { method: 'POST', body: {
    gateway_id: health.gateway_id, package_id: 'schema.probe', package_version: '2.0.0' } });
  await api('/api/gateway/service-plugins/uninstall', { method: 'POST', body: {
    gateway_id: health.gateway_id, package_id: 'schema.probe', package_version: '3.0.0' } });
  const schemas = await api('/api/gateway/service-plugins/configuration-schemas', { method: 'POST', body: { gateway_id: health.gateway_id, workspace_id: b.workspace_id } });
  assert(!schemas.plugins.some(plugin => plugin.package_id === 'schema.probe' && plugin.package_version === '2.0.0'));
  await api('/api/gateway/service-plugins/uninstall', { method: 'POST', body: {
    gateway_id: health.gateway_id, package_id: 'schema.probe', package_version: '1.0.0' } }, 409);
  const runtime = await api('/api/gateway/service-plugins/status', { method: 'POST', body: { gateway_id: health.gateway_id } });
  assert(runtime.plugins.filter(plugin => plugin.binding.package_id === 'schema.probe').every(plugin => !plugin.instance));
  result.zero_runtime = true;
  assert(result.writes.some(write => write.workspace_id === a.workspace_id));
  assert(result.writes.some(write => write.workspace_id === b.workspace_id));
  assert(result.writes.every(write => write.gateway_id === health.gateway_id));
  assert.deepEqual(result.oldHostRequests, []); assert.deepEqual(result.pageErrors, []);
  result.phase = 'complete';
} catch (error) {
  result.error = error.stack ?? String(error);
  if (page) await page.screenshot({ path: `${input.artifacts}/settings-schema-versions-failure.png`, fullPage: true });
  throw error;
} finally {
  await writeFile(input.resultPath, JSON.stringify(result, null, 2) + '\n');
  await browser?.close();
}
