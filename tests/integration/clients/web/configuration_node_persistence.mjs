import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { parse } from 'jsonc-parser';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_NODE_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须设置正式测试产物目录');
const result = { phase: 'initialization', pageErrors: [], consoleErrors: [], initialization: [], writes: [], saves: [], inspections: [] };
let browser, context, page, failure;
const cleanup = [];
const profile = (document, reference) => {
  assert(reference.startsWith('#/profiles/'), `预期私有 profile：${reference}`);
  return document.profiles[reference.slice('#/profiles/'.length).replaceAll('~1', '/').replaceAll('~0', '~')];
};
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  cleanup.push(() => browser.close());
  context = await browser.newContext({ viewport: { width: 1400, height: 950 } });
  cleanup.push(() => context.close());
  page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  page.on('request', request => {
    if (request.method() === 'PUT' && request.url().includes('/config/sources/')) result.writes.push(request.postDataJSON());
  });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const api = url => page.evaluate(async ({ url, owner }) => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data;
    const response = await fetch(url, { headers: { 'X-Local-Token': credential.token,
      'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } });
    const envelope = await response.json();
    if (!response.ok || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return { data: envelope.data, status: response.status, requestId: envelope.request_id };
  }, { url, owner: fixture });
  const openDetails = async details => { if (!(await details.evaluate(element => element.open))) await details.locator(':scope > summary').click(); };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push(await api(url));
  assert.equal(result.initialization[0].data.gateway_id, fixture.gatewayId);
  assert.equal(result.initialization[1].data.active_workspace.workspace_id, fixture.workspaceId);
  const location = { version: 2, gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
    source_key: 'workspace_root_mutable_override', name: '配置节点持久化' };
  const sourceUrl = '/api/v1/config/sources/' + location.source_key;
  const originalDocument = (await api(sourceUrl)).data;
  await writeFile(path.join(artifacts, `${fixture.phase}-source-document.json`), JSON.stringify(originalDocument, null, 2) + '\n');
  const form = page.locator('.configuration-entities[data-definition-kind="providers"]');
  const article = form.locator('article[data-definition-id]');
  const providerNode = article.locator('.configuration-provider-connection > .configuration-node-reference');
  const model = id => article.locator(`[data-model-id="${id}"]`);
  const modelNode = id => model(id).locator('.configuration-node-reference');
  const openProvider = async id => {
    await command('boxteam.settings.modelsProviders', location, id);
    await expect(article).toHaveAttribute('data-definition-id', id);
    await openDetails(article.locator('.configuration-provider-connection'));
    await openDetails(providerNode);
    await openDetails(article.locator('.configuration-model-probe > details').first());
    await form.getByRole('checkbox', { name: '连接编辑结束后自动获取一次模型', exact: true }).uncheck();
  };
  const openModel = async id => {
    await article.getByRole('navigation', { name: '模型列表', exact: true }).getByRole('button', { name: id === 'default' ? 'upstream-main' : 'upstream-sibling', exact: true }).click();
    await openDetails(model(id).locator('.configuration-model-overrides'));
    await openDetails(modelNode(id));
  };
  const inspect = async node => {
    const response = page.waitForResponse(value => {
      const resource = new URL(value.url()).pathname;
      return value.request().method() === 'GET' && resource === '/api/v1/config/llm/inspection'
        || value.request().method() === 'POST' && resource.includes('/config/sources/') && resource.endsWith('/preflight');
    });
    await node.getByRole('button', { name: '预检并查看最终选项', exact: true }).click();
    const receipt = await response;
    const body = await receipt.json();
    assert.equal(body.request_id, receipt.headers()['x-request-id']);
    result.inspections.push({ status: receipt.status(), requestId: body.request_id,
      method: receipt.request().method(), path: new URL(receipt.url()).pathname });
    return { status: receipt.status(), body };
  };
  const inspectOkay = async node => {
    const receipt = await inspect(node);
    assert.equal(receipt.status, 200, JSON.stringify(receipt.body));
    const data = receipt.body.data, published = 'active_digest' in data;
    const owner = published ? data : data.document;
    assert.equal(owner.gateway_id, fixture.gatewayId);
    assert.equal(owner.workspace_id, fixture.workspaceId);
    if (published) {
      assert.equal(typeof data.active_revision, 'number');
      assert.equal(typeof data.active_digest, 'string');
      assert(data.active_digest.length > 0);
    } else assert.equal(owner.source_key, location.source_key);
    await expect(node.getByText(published ? '正在显示已发布的配置快照' : '本次草稿预检通过', { exact: true })).toBeVisible();
    return receipt.body.data;
  };
  const temperature = async (id, value) => {
    const node = modelNode(id);
    await inspectOkay(node);
    const parameters = model(id).locator('.configuration-option-section').filter({ has: page.locator(':scope > summary', { hasText: '请求参数' }) });
    await openDetails(parameters);
    const parameter = model(id).locator('[data-parameter="temperature"]');
    await parameter.locator('input[name="value"]').fill(String(value));
    await parameter.getByRole('button', { name: '应用', exact: true }).click();
    await expect(parameter.locator('input[name="value"]')).toHaveValue(String(value));
  };
  const save = async () => {
    await command('notifications.hideToasts');
    await form.getByRole('button', { name: '保存来源', exact: true }).click();
    const modal = page.locator('.monaco-dialog-box[role="dialog"]');
    await expect(modal).toContainText('保存目标配置来源');
    const response = page.waitForResponse(value => value.request().method() === 'PUT' && new URL(value.url()).pathname === sourceUrl);
    await modal.getByRole('button', { name: '保存来源', exact: true }).click();
    const saved = await response, receipt = await saved.json();
    assert.equal(saved.status(), 200, JSON.stringify(receipt));
    assert(receipt.data.edit.source_saved);
    assert.equal(receipt.request_id, saved.headers()['x-request-id']);
    result.saves.push({ requestId: receipt.request_id, edit: receipt.data.edit });
    await expect(page.locator('.tabs-container .tab.active')).not.toHaveClass(/\bdirty\b/);
    return parse((await api(sourceUrl)).data.jsonc);
  };
  await openProvider('primary');
  if (fixture.phase === 'save') {
    const original = parse(originalDocument.jsonc);
    result.phase = 'provider-private-edit';
    await article.getByRole('textbox', { name: /^API 地址/ }).first().fill(fixture.endpoint);
    await expect(providerNode.getByRole('textbox', { name: '配置引用（config_ref）', exact: true })).toHaveValue(/^#\/profiles\/private-/);
    await openModel('default');
    result.phase = 'model-private-parameter-edit';
    await temperature('default', 0.75);
    assert.equal(result.writes.length, 0, '表单修改与预检不能自动保存来源');
    result.phase = 'first-save';
    const saved = await save();
    const providers = saved.settings.workspace.llm.providers;
    assert.deepEqual(providers.peer, original.settings.workspace.llm.providers.peer);
    assert.deepEqual(providers.primary.models.sibling, original.settings.workspace.llm.providers.primary.models.sibling);
    assert.deepEqual(saved.profiles, { ...original.profiles,
      [providers.primary.config_ref.slice('#/profiles/'.length)]: { extends: fixture.externalProvider, settings: { model_options: { endpoint: fixture.endpoint } } },
      [providers.primary.models.default.config_ref.slice('#/profiles/'.length)]: { extends: fixture.externalModel,
        settings: { model_options: { request_options: { parameters: { temperature: 0.75 } } } } },
    }, '首写只增加当前对象的私有子节点与单个选项');
    result.phase = 'reference-error-repair';
    const reference = modelNode('default').getByRole('textbox', { name: '配置引用（config_ref）', exact: true });
    const validReference = await reference.inputValue();
    await reference.fill('#/profiles/missing-node');
    const invalid = await inspect(modelNode('default'));
    assert(invalid.status >= 400, '无效 config_ref 必须明确拒绝');
    await expect(modelNode('default').getByRole('alert')).toBeVisible();
    await reference.fill(validReference);
    await inspectOkay(modelNode('default'));
    const parent = modelNode('default').getByRole('textbox', { name: '父节点引用（extends）', exact: true });
    const validParent = await parent.inputValue();
    await parent.fill('#/profiles/missing-parent');
    const badParent = await inspect(modelNode('default'));
    assert(badParent.status >= 400, '无效 extends 必须明确拒绝');
    await parent.fill(validParent);
    await inspectOkay(modelNode('default'));
    assert.equal(result.writes.length, 1, '引用错误与修正不能隐式保存');
    result.phase = 'provider-copy';
    await command('notifications.hideToasts');
    await article.locator('.configuration-definition-heading > .configuration-actions-button').click();
    await page.locator('.monaco-menu-container:visible').getByRole('menuitem', { name: '复制 Provider', exact: true }).click({ delay: 150 });
    const quick = page.locator('.quick-input-widget');
    await expect(quick).toBeVisible();
    await quick.locator('input').first().fill('节点持久化副本');
    await quick.locator('input').first().press('Enter');
    await expect(article).not.toHaveAttribute('data-definition-id', 'primary');
    result.copyId = await article.getAttribute('data-definition-id');
    await openDetails(article.locator('.configuration-provider-connection'));
    await openDetails(providerNode);
    await openModel('default');
    await temperature('default', 0.9);
    result.phase = 'copy-save';
    const copied = await save(), definitions = copied.settings.workspace.llm.providers;
    assert.deepEqual(definitions.primary, providers.primary);
    assert.deepEqual(definitions.peer, providers.peer);
    assert.notEqual(definitions[result.copyId].config_ref, definitions.primary.config_ref);
    for (const id of ['default', 'sibling']) assert.notEqual(definitions[result.copyId].models[id].config_ref, definitions.primary.models[id].config_ref);
    assert.equal(profile(copied, definitions[result.copyId].models.default.config_ref).settings.model_options.request_options.parameters.temperature, 0.9);
    assert.equal(profile(copied, definitions.primary.models.default.config_ref).settings.model_options.request_options.parameters.temperature, 0.75);
    assert.equal(profile(copied, definitions[result.copyId].models.sibling.config_ref).extends, fixture.externalProvider);
    result.savedDocument = copied;
  } else {
    result.phase = 'cold-provider-read';
    await expect(article.getByRole('textbox', { name: /^API 地址/ }).first()).toHaveValue(fixture.endpoint);
    const providerInspection = await inspectOkay(providerNode);
    assert.equal(providerInspection.provider_options.options.endpoint, fixture.endpoint);
    result.phase = 'cold-model-read';
    await openModel('default');
    const restored = await inspectOkay(modelNode('default'));
    assert.equal(restored.model_options.options.request_options.parameters.temperature, 0.75);
    assert(restored.model_options.chain.some(item => item.reference === fixture.externalModel));
    await openProvider(fixture.copyId);
    await openModel('default');
    const restoredCopy = await inspectOkay(modelNode('default'));
    assert.equal(restoredCopy.model_options.options.request_options.parameters.temperature, 0.9);
    result.phase = 'narrow-layout';
    await page.setViewportSize({ width: 720, height: 900 });
    const control = modelNode('default').getByRole('textbox', { name: '父节点引用（extends）', exact: true });
    await control.scrollIntoViewIfNeeded();
    await expect(control).toBeVisible();
    const bounds = await control.boundingBox();
    assert(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 721, JSON.stringify(bounds));
    await page.screenshot({ path: path.join(artifacts, 'cold-read-narrow.png') });
    assert.equal(result.writes.length, 0, '冷读和布局检查不能写配置');
  }
  assert.deepEqual(result.pageErrors, []);
  result.phase = 'completed';
} catch (error) {
  failure = error;
  result.error = error.stack ?? String(error);
  if (page) {
    try { await page.screenshot({ path: path.join(artifacts, `${fixture.phase}-failure.png`) }); }
    catch (diagnostic) { result.screenshotError = String(diagnostic); }
  }
} finally {
  const errors = [];
  for (const close of cleanup.reverse()) {
    try { await close(); } catch (error) { errors.push(error); }
  }
  result.cleanupErrors = errors.map(String);
  try { await writeFile(path.join(artifacts, `${fixture.phase}-result.json`), JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { errors.push(error); }
  if (failure || errors.length) throw new AggregateError([...(failure ? [failure] : []), ...errors], '配置节点浏览器验收失败');
}
