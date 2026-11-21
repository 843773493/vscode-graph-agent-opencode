import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_MODEL_TARGET_FIXTURE);
assert(fixture.artifacts, '必须指定正式测试产物目录');
const scope = TestRunContext.fromTestFile('tests/integration/clients/web/test_model_target_isolation.py');
const result = { phase: 'initialization', pageErrors: [], sessions: [], probes: [], initialization: [] };
let page, token, failure;
const quick = () => page.locator('.quick-input-widget');
const row = model => quick().locator('.monaco-list-row').filter({
  has: page.locator('.label-name', { hasText: new RegExp(`^same-${model}$`) }),
});
async function api(url, gatewayId, init = {}) {
  return page.evaluate(async ({ url, gatewayId, token, workspaceId, init }) => {
    const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json',
      'X-Local-Token': token, ...(gatewayId ? { 'X-BoxTeam-Target-Gateway-Id': gatewayId,
        'X-BoxTeam-Workspace-Id': workspaceId } : {}) } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return { data: body.data, requestId: body.request_id };
  }, { url, gatewayId, token, workspaceId: fixture.workspaceId, init });
}
const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
async function open(index) {
  const session = result.sessions[index];
  await command('_workbench.open', `boxteam-thread://${fixture.gatewayIds[index]}/${fixture.workspaceId}/${session.session_id}/${session.thread_id}`,
    [undefined, { pinned: true }]);
  const thread = page.locator(`.editor-group-container.active section.boxteam-thread[data-session-id="${session.session_id}"]`);
  await expect(thread.getByTestId('composer-input')).toBeEditable();
  return thread;
}
async function picker(index) {
  const thread = await open(index);
  await thread.locator('.composer-model-pill').click();
  await expect(quick()).toBeVisible();
  await expect(row('backup')).toBeVisible();
  return thread;
}
async function action(model, name) {
  await row(model).hover();
  await row(model).getByRole('button', { name, exact: true }).click();
}
async function verifyPreferences() {
  await picker(0);
  await expect(row('main')).toHaveCount(0);
  await row('backup').hover();
  await expect(row('backup').getByRole('button', { name: '取消收藏', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await picker(1);
  await expect(row('main')).toBeVisible();
  await row('backup').hover();
  await expect(row('backup').getByRole('button', { name: '收藏模型', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  scope.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1400, height: 950 } });
  scope.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30_000 });
  await page.waitForFunction(() => Boolean(globalThis.workbench?.commands?.executeCommand));
  token = await page.evaluate(async () => (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    const receipt = await api(url, url.includes('/api/v1/') ? fixture.gatewayIds[0] : undefined);
    result.initialization.push({ url, requestId: receipt.requestId });
  }
  for (const [index, gatewayId] of fixture.gatewayIds.entries()) {
    const created = await api('/api/v1/sessions', gatewayId, { method: 'POST',
      body: JSON.stringify({ title: `同名模型归属 ${index}`, agent_id: 'default' }) });
    const main = await api(`/api/v1/sessions/${created.data.session_id}/main-thread`, gatewayId);
    result.sessions.push({ session_id: created.data.session_id, thread_id: main.data.thread_id });
  }
  result.phase = 'local-preferences';
  const local = await picker(0);
  await row('backup').click();
  await expect(local.locator('.composer-model-label')).toHaveText('same-backup');
  await picker(0);
  await action('backup', '收藏模型');
  await action('main', '隐藏模型');
  await expect(row('main')).toHaveCount(0);
  await page.keyboard.press('Escape');
  result.phase = 'same-uuid-owner-isolation';
  await verifyPreferences();
  await command('boxteam.flushViewState');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => Boolean(globalThis.workbench?.commands?.executeCommand));
  await verifyPreferences();
  result.preferencesRestored = true;
  result.phase = 'settings-and-native-credentials';
  for (const index of [1, 0]) {
    await picker(index);
    await action('backup', '模型与 Provider');
    const form = page.locator('.configuration-entities[data-definition-kind="providers"]');
    await expect(form.locator('article[data-definition-id="primary"] section[data-model-id="backup"]')).toBeVisible();
    const panel = form.getByRole('region', { name: '发现与测试模型', exact: true });
    await expect(panel).toContainText(fixture.gatewayIds[index]);
    await expect(panel).toContainText(fixture.workspaceId);
    const started = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/config/llm/test');
    await panel.getByRole('button', { name: '测试配置模式', exact: true }).click();
    const response = await started;
    assert.equal(response.status(), 200, await response.text());
    const request = response.request(), headers = request.headers(), body = request.postDataJSON();
    assert.equal(headers['x-boxteam-target-gateway-id'], fixture.gatewayIds[index]);
    assert.equal(headers['x-boxteam-workspace-id'], fixture.workspaceId);
    assert.equal(body.gateway_id, fixture.gatewayIds[index]);
    assert.equal(body.workspace_id, fixture.workspaceId);
    assert.equal(body.provider_id, 'primary');
    assert.equal(body.model_id, 'backup');
    assert.equal(body.mode, 'active');
    const completed = panel.locator('.configuration-probe-result[data-probe-model="backup"]');
    await expect(completed.getByRole('status')).toContainText('本次通过');
    await expect(completed.locator('pre')).toHaveText('Hello from the configured model.');
    await expect(completed).toContainText('vault r1');
    const requestId = response.headers()['x-request-id'];
    assert(requestId);
    result.probes.push({ gatewayId: fixture.gatewayIds[index], requestId });
  }
  result.phase = 'narrow-composer';
  await page.setViewportSize({ width: 460, height: 800 });
  await expect((await open(1)).getByTestId('composer-input')).toBeEditable();
  assert.deepEqual(result.pageErrors, []);
  result.phase = 'complete';
} catch (error) { failure = error; result.failure = String(error.stack ?? error); }
finally {
  const errors = failure ? [failure] : [];
  try { if (page) await page.screenshot({ path: path.join(fixture.artifacts, 'model-target-isolation.png') }); }
  catch (error) { errors.push(error); }
  try { await writeFile(path.join(fixture.artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, `双 Gateway 模型验收失败: ${result.phase}`);
}
