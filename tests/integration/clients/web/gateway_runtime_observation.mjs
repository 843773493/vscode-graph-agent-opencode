import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { applyEdits, modify } from 'jsonc-parser';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';

const fixture = JSON.parse(process.env.BOXTEAM_GATEWAY_OBSERVATION_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须指定正式产物目录');
const result = { phase: 'initialization', pageErrors: [], unexpectedCalls: [], initialization: [], apiSamples: [], diagnosticsReads: 0 };
let browser, page;
try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(15000);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (/tool-approvals|\/messages(?:$|\?)|\/chat\/completions|\/responses(?:$|\?)/.test(pathname)) result.unexpectedCalls.push(pathname);
    if (pathname === '/api/gateway/diagnostics') result.diagnosticsReads++;
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const api = url => page.evaluate(async url => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const started = performance.now();
    const response = await fetch(url, { headers: { 'X-Local-Token': token } });
    const text = await response.text();
    const elapsedMs = performance.now() - started, body = JSON.parse(text);
    if (!response.ok || !body.request_id || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(text);
    return { data: body.data, elapsedMs, bytes: new TextEncoder().encode(text).length, requestId: body.request_id };
  }, url);
  const control = page.locator('.settings-editor:visible');
  const panel = control.getByTestId('gateway-diagnostics-panel');
  const close = () => command('workbench.action.closeActiveEditor');
  const openDiagnostics = async workspaceId => {
    await command('boxteam.gateway.diagnostics', undefined, workspaceId);
    await expect(panel.getByRole('button', { name: '复制诊断摘要', exact: true })).toBeEnabled();
  };
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  for (const url of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) result.initialization.push(await api(url));
  assert.equal(result.initialization[0].data.gateway_id, fixture.gatewayId);
  assert.equal(result.initialization[2].data.workspace_id, fixture.workspaceId);
  await expect(page.getByTestId(`workspace-node-${JSON.stringify([fixture.gatewayId, fixture.workspaceId])}`)).toBeVisible();

  result.phase = 'save-existing-gateway-settings';
  const sourceKey = 'gateway_mutable_override';
  const sourceUrl = '/api/gateway/config/sources/' + sourceKey;
  const target = { gateway_id: fixture.gatewayId, name: 'Gateway', connection_kind: 'local' };
  const settings = command('boxteam.settings.openTarget', target, {});
  const quick = page.locator('.quick-input-widget');
  await expect(quick).toBeVisible();
  await quick.locator('.monaco-list-row').filter({ hasText: sourceKey }).click();
  await settings;
  const categories = page.locator('.editor-group-container.active .settings-toc-container');
  for (const title of ['产品日志与保留', 'Gateway 遥测', 'Gateway 运行统计']) await expect(categories).toContainText(title);
  const source = (await api(sourceUrl)).data;
  let jsonc = source.jsonc;
  for (const field of ['telemetry', 'statistics']) jsonc = applyEdits(jsonc, modify(jsonc, [field, 'runtime', 'enabled'], true,
    { formattingOptions: { insertSpaces: true, tabSize: 2 } }));
  await command('boxteam.settings.openSource', { location: { version: 2, gateway_id: fixture.gatewayId, source_key: sourceKey, name: 'Gateway' } });
  const lines = page.locator('.editor-group-container.active .editor-instance:visible .monaco-editor .view-lines').last();
  await lines.click(); await page.keyboard.press('Control+A');
  await page.evaluate(text => navigator.clipboard.writeText(text), jsonc); await page.keyboard.press('Control+V');
  const saved = command('workbench.action.files.save');
  const modal = page.locator('.monaco-dialog-box[role="dialog"]');
  await expect(modal).toContainText('保存目标配置来源');
  await expect(modal).toContainText(fixture.gatewayId);
  const received = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname === sourceUrl);
  await modal.getByRole('button', { name: '保存来源', exact: true }).click();
  const response = await received, body = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(body));
  assert.equal(body.request_id, response.headers()['x-request-id']);
  assert(body.data.edit.source_saved);
  await saved;
  result.appliedSettings = ['telemetry', 'statistics'].every(field => body.data.effective[field].runtime.enabled);
  assert(result.appliedSettings);

  result.phase = 'actual-runtime-and-safe-copy';
  const opened = performance.now();
  await openDiagnostics();
  const observation = panel.locator('details').filter({ has: page.locator('summary', { hasText: '遥测与运行统计' }) });
  await observation.locator('summary').click();
  await expect(observation).toContainText('遥测：已启用');
  await expect(observation).toContainText('运行统计：已启用');
  await expect(observation).toContainText('HTTP 请求：');
  result.firstAvailableMs = performance.now() - opened;
  assert(result.firstAvailableMs <= 3000, `诊断详情首次可用 ${result.firstAvailableMs}ms`);
  await panel.getByRole('button', { name: '复制诊断摘要', exact: true }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  const summary = JSON.parse(copied);
  assert.equal(summary.gateway_id, fixture.gatewayId);
  assert(summary.processes.some(process => process.service === 'gateway' && process.state.runtime_instance_id));
  const forbidden = new Set(['tail', 'error', 'jsonc', 'api_key', 'token', 'samples', 'payload']);
  const inspect = value => {
    if (!value || typeof value !== 'object') return;
    for (const [key, child] of Object.entries(value)) { assert(!forbidden.has(key), `诊断复制包含正文/秘密字段 ${key}`); inspect(child); }
  };
  inspect(summary);
  assert(!('control_directory' in summary) && !('log_directory' in summary));
  result.summarySafe = true;
  result.summary = summary;
  const storage = panel.locator('details').filter({ has: page.locator('summary', { hasText: '存储位置与保留' }) });
  await storage.locator('summary').click();
  await expect(storage).toContainText(fixture.gatewayControl);
  await expect(storage).toContainText(fixture.gatewayLogs);
  await expect(storage).toContainText('目标机器');
  result.phase = 'warm-diagnostics-and-release';
  // 本切片只改变已有诊断详情展示，选该唯一详情 API；不重复采样其它列表。
  for (let index = 0; index < 21; index++) {
    const sample = await api('/api/gateway/diagnostics');
    assert.equal(sample.data.gateway_id, fixture.gatewayId);
    assert.equal(sample.data.control_directory, fixture.gatewayControl);
    assert.equal(sample.data.log_directory, fixture.gatewayLogs);
    assert(sample.data.observation.telemetry.enabled && sample.data.observation.statistics.enabled);
    const { data: _body, ...measurement } = sample;
    result.apiSamples.push(measurement);
  }
  const warm = result.apiSamples.slice(1).map(sample => sample.elapsedMs).sort((a, b) => a - b);
  result.warm = { p50: warm[9], p95: warm[18], max: warm[19] };
  assert(result.warm.p95 <= 500 && result.warm.max <= 1500);
  await page.screenshot({ path: path.join(artifacts, 'gateway-runtime-observation.png') });
  await close(); await expect(control).toBeHidden();
  const reads = result.diagnosticsReads;
  await page.waitForTimeout(600);
  assert.equal(result.diagnosticsReads, reads, '关闭后不得继续读诊断');
  result.phase = 'original-owner-storage-and-retention';
  await command('boxteam.workspace.details', { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId });
  const details = page.getByRole('dialog', { name: '工作区详情', exact: true });
  await expect(details).toContainText(fixture.workspaceData);
  assert.equal(result.initialization[2].data.data_directory, fixture.workspaceData);
  await details.getByRole('button', { name: '关闭工作区详情', exact: true }).first().click();
  await openDiagnostics(fixture.workspaceId);
  await expect(control.getByRole('combobox', { name: '配置范围', exact: true })).toHaveValue('后端默认设置');
  const settingsTabs = await page.locator('.tabs-container .tab').count();
  await storage.locator('summary').click();
  await panel.getByRole('button', { name: '打开 Gateway 日志保留设置', exact: true }).click();
  await expect(control.locator('.settings-management-view')).toBeHidden();
  const header = page.locator('.settings-context-label:visible');
  await expect(header).toContainText(fixture.gatewayId);
  await expect(page.locator('.settings-category-context:visible select[aria-label="配置范围"] option:checked')).toHaveText('Gateway 设置');
  const sources = (await api('/api/gateway/config/sources')).data;
  const selectedSource = sources.sources.find(item => item.source_key === sourceKey);
  assert(selectedSource && sources.schema_uri);
  await header.locator('summary').click();
  await header.getByRole('button', { name: '查看配置来源位置', exact: true }).click();
  await expect(quick).toContainText(selectedSource.uri ?? '原 owner 未提供');
  await expect(quick).toContainText(sources.schema_uri);
  await page.keyboard.press('Escape');
  result.storageLocations = { workspace: fixture.workspaceData, control: fixture.gatewayControl, logs: fixture.gatewayLogs,
    source: selectedSource.uri, schema: sources.schema_uri };
  assert.equal(await page.locator('.tabs-container .tab').count(), settingsTabs, '配置范围跳转不能另开设置 Tab');
  result.retentionOwner = fixture.gatewayId;
  result.retentionFromWorkspace = true;
  result.phase = 'original-product-about';
  const about = command('workbench.action.showAboutDialog');
  await expect(modal).toContainText(fixture.product.nameLong);
  await expect(modal).toContainText(fixture.product.version);
  await expect(modal).toContainText(fixture.product.commit);
  await expect(modal).toContainText(fixture.product.date);
  await page.keyboard.press('Escape');
  await about;
  result.product = fixture.product;
  assert.deepEqual(result.pageErrors, []); assert.deepEqual(result.unexpectedCalls, []);
  result.phase = 'completed';
} catch (error) {
  result.error = String(error.stack ?? error); process.exitCode = 1;
  if (page) try { await page.screenshot({ path: path.join(artifacts, 'failure.png') }); }
  catch (captureError) { result.captureError = String(captureError); }
} finally {
  try { await browser?.close(); } catch (error) { result.closeError = String(error); process.exitCode = 1; }
  await writeFile(path.join(artifacts, 'result.json'), JSON.stringify(result, null, 2));
}
