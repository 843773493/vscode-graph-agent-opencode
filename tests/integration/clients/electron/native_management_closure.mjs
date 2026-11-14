import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { launchElectron } from './launch.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`原生管理闭环缺少 ${name}`);
  return value;
};
const owner = JSON.parse(required('BOXTEAM_ELECTRON_OWNER'));
const artifacts = required('BOXTEAM_ELECTRON_ARTIFACTS');
const workspace = required('BOXTEAM_NATIVE_MANAGEMENT_WORKSPACE');
const token = required('BOXTEAM_ELECTRON_TOKEN');
const resultPath = path.join(artifacts, 'native-management.json');
const run = TestRunContext.fromTestFile('tests/integration/clients/electron/test_native_management_closure.py');
const result = { classification: 'Integration', phase: 'initialize', writes: [], backendValues: [], pageErrors: [],
  boundary: '真实 Gateway／Workspace 来源保存与 Provider 管理；复用不可变 development ASAR，不代表当前源码完整发行验收' };
let page, failure, closing = false;

async function readSource() {
  const response = await page.evaluate(async ({ owner, token }) => {
    const response = await fetch('/api/v1/config/sources/workspace_root_mutable_override', { headers: {
      'X-Local-Token': token, 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id,
    } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return body;
  }, { owner, token });
  return response;
}

try {
  const host = await launchElectron({ run, result, usePage(value) {
    page = value;
    const number = result.native.launches.length;
    page.setDefaultTimeout(30_000);
    page.on('pageerror', error => result.pageErrors.push({ launch: number, message: error.message }));
    page.on('console', message => { if (message.type() === 'error') result.pageErrors.push({ launch: number, console: message.text() }); });
    page.on('requestfailed', request => result.pageErrors.push({ launch: number, request: request.url(), failure: request.failure()?.errorText, closing }));
    page.on('request', request => {
      if (request.method() === 'PUT' && new URL(request.url()).pathname.startsWith('/api/v1/config/sources/')) {
        result.writes.push({ launch: number, url: request.url(), body: request.postDataJSON() });
      }
    });
  }, async observe(label, operation) {
    if (label !== 'electron.nativeClose') return operation();
    closing = true;
    try { return await operation(); } finally { closing = false; }
  } });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  result.phase = 'settings-source';
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const location = { version: 2, gateway_id: owner.gateway_id, workspace_id: owner.workspace_id,
    source_key: 'workspace_root_mutable_override', name: 'Electron 管理工作区' };
  const sourceUrl = '/api/v1/config/sources/workspace_root_mutable_override';
  await command('boxteam.settings.modelsProviders', location);
  const form = page.locator('.configuration-entities[data-definition-kind="providers"]');
  await expect(form).toBeVisible();
  await form.getByRole('button', { name: '新增 Provider', exact: true }).click();
  const quick = page.locator('.quick-input-widget');
  const input = async value => { await expect(quick).toBeVisible(); const field = quick.locator('input').first(); await field.fill(value); await field.press('Enter'); };
  await input('native-management-provider-12');
  await input('native-management-model');
  await input('https://native-management.example/v1');
  await input('openai');
  await quick.locator('.monaco-list-row').filter({ hasText: 'chat_completions' }).click();
  await input('${NATIVE_MANAGEMENT_KEY}');
  const confirmation = page.locator('.monaco-dialog-box[role="dialog"]');
  if (await confirmation.isVisible()) await confirmation.getByRole('button', { name: '新增 Provider', exact: true }).click();
  await expect(form.locator('[data-definition-id="native-management-provider-12"]')).toBeVisible();
  await form.getByLabel('模型名称', { exact: true }).fill('native-management-model-saved');
  const tab = page.locator('.tabs-container .tab.active');
  await expect(tab).toHaveClass(/dirty/);
  assert.equal(result.writes.length, 0, '编辑来源不能自动保存');
  await form.getByRole('button', { name: '保存来源', exact: true }).click();
  await expect(confirmation).toContainText('保存目标配置来源');
  const savedResponse = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname === sourceUrl);
  await confirmation.getByRole('button', { name: '保存来源', exact: true }).click();
  const saved = await savedResponse, envelope = await saved.json();
  assert.equal(saved.status(), 200);
  assert.equal(envelope.data.edit.source_saved, true);
  assert.equal(envelope.request_id, saved.headers()['x-request-id']);
  await expect(tab).not.toHaveClass(/dirty/);
  const current = await readSource();
  const stored = current.data.source_effective.llm.providers.find(item => item.id === 'native-management-provider-12');
  assert.equal(stored.model, 'native-management-model-saved');
  assert(stored.api_key.startsWith('boxteam-redacted:'), '公开来源读取仍应脱敏凭据');
  const sourceWrite = result.writes.at(-1).body;
  assert.equal(sourceWrite.gateway_id, owner.gateway_id);
  assert.equal(sourceWrite.workspace_id, owner.workspace_id);
  result.backendValues.push({ requestId: envelope.request_id, gatewayId: sourceWrite.gateway_id,
    workspaceId: sourceWrite.workspace_id, model: stored.model, apiKeyReference: stored.api_key, edit: envelope.data.edit });
  const physicalConfig = path.join(workspace, '.boxteam/workspace.jsonc');
  const physicalText = await readFile(physicalConfig, 'utf8');
  assert(physicalText.includes('native-management-model-saved'), `目标工作区配置文件未保存：${physicalConfig}`);
  result.physicalSource = { path: physicalConfig, sha256: createHash('sha256').update(physicalText).digest('hex') };
  result.phase = 'saved-clean';
  await host.reload();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  result.phase = 'reopened';
  await command('boxteam.settings.modelsProviders', location, 'native-management-provider-12');
  await expect(page.locator('.configuration-entities[data-definition-kind="providers"] [data-definition-id="native-management-provider-12"]')).toBeVisible();
  await expect(page.locator('.configuration-entities[data-definition-kind="providers"] [data-definition-id="native-management-provider-12"]').getByLabel('模型名称', { exact: true }))
    .toHaveValue('native-management-model-saved');
  const reloaded = await readSource();
  const reloadedProvider = reloaded.data.source_effective.llm.providers.find(item => item.id === 'native-management-provider-12');
  assert.equal(reloadedProvider.model, 'native-management-model-saved');
  result.backendValues.push({ requestId: reloaded.request_id, gatewayId: owner.gateway_id,
    workspaceId: owner.workspace_id, model: reloadedProvider.model });
  result.recoveredOwner = owner;
  assert.equal(result.writes.length, 1, '保存仅一次提交，重启不能重放写入');
  await host.leave();
  result.phase = 'complete';
} catch (error) { failure = error; result.error = error?.stack ?? String(error); }
finally {
  try { await run.close(); }
  catch (error) { failure ??= error; result.closeError = error?.stack ?? String(error); }
  result.phase = failure ? 'failed' : result.phase;
  await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
