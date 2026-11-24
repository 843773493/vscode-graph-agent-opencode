import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { Perf } from '../../../harness/js/performance.mjs';
import { observePage } from '../../../harness/js/browser-performance.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_CONFIG_LATENCY_FIXTURE);
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
assert(artifacts, '必须显式指定性能产物目录');
const cleanup = [];
const perf = new Perf({ projectRoot: process.cwd(), performanceDir: artifacts, runRoot: artifacts,
  testId: 'e2e/clients/web/configuration_inheritance_latency', addCleanup: (label, run) => cleanup.push([label, run]) }, { mode: 'core' });
const result = { phase: 'startup', httpSamples: [], settingsMs: [], uiInspections: [], pageErrors: [], consoleErrors: [],
  forbiddenExecutions: [], workload: { providers: 20, modelsPerProvider: 10, sharedAncestor: true, editors: fixture.httpOnly ? 5 : 6,
    mode: fixture.httpOnly ? 'http-only' : fixture.firstSettingsOnly ? 'first-settings-only' : 'full' },
  boundary: '真实Workbench/Gateway/Backend，配置准备后首个inspection为冷请求；不发送模型、目录发现或代理探测，无HTTP响应替身' };
let browser, page, failure, token, firstSettingsStarted;
const requestStarts = new Map();
const summary = samples => {
  const values = [...samples].sort((a, b) => a - b);
  return { count: values.length, p95: values[Math.ceil(values.length * 0.95) - 1], max: values.at(-1) };
};
const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
const openDetails = async node => { if (!(await node.evaluate(element => element.open))) await node.locator(':scope > summary').click(); };

try {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  cleanup.push(['Chromium', () => browser.close()]);
  const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
  cleanup.push(['BrowserContext', () => context.close()]);
  page = await context.newPage();
  page.setDefaultTimeout(15000);
  const observer = await observePage(page, perf, context);
  if (observer.close) cleanup.push(['性能观察', () => observer.close()]);
  page.on('pageerror', error => result.pageErrors.push(error.message));
  page.on('console', item => { if (item.type() === 'error') result.consoleErrors.push(item.text()); });
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/config/llm/inspection') requestStarts.set(request, {
      started: performance.now(), wallStartedMs: Date.now(),
    });
    if (request.method() !== 'GET' && /\/config\/llm\/(discover|test)|\/(messages|jobs)(\/|$)/.test(url.pathname)) {
      result.forbiddenExecutions.push(url.pathname);
    }
  });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => !!globalThis.workbench?.commands?.executeCommand);
  token = await page.evaluate(async () => (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token);
  const api = async url => {
    const receipt = await page.evaluate(async ({ url, token, owner }) => {
      const started = performance.now(), wallStartedMs = Date.now();
      const response = await fetch(url, { cache: 'no-store', headers: { 'X-Local-Token': token,
        'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId } });
      const text = await response.text(), body = JSON.parse(text);
      if (!response.ok || !body.request_id || body.request_id !== response.headers.get('x-request-id')) {
        throw new Error(JSON.stringify({ url, status: response.status, body }));
      }
      return { data: body.data, elapsedMs: performance.now() - started, wallStartedMs, wallFinishedMs: Date.now(),
        requestId: body.request_id, bytes: new TextEncoder().encode(text).length, url };
    }, { url, token, owner: fixture });
    result.httpSamples.push({ ...receipt, data: undefined, phase: result.phase });
    perf.record('end', 'http.config.inspection', { stage_id: crypto.randomUUID(), status: 'passed',
      elapsed_ns: Math.round(receipt.elapsedMs * 1e6), request_id: receipt.requestId, response_bytes: receipt.bytes, inclusive: true }, { phase: result.phase });
    return receipt;
  };
  const health = await api('/api/gateway/health'), registry = await api('/api/gateway/workspaces'), workspace = await api('/api/v1/workspace');
  assert.equal(health.data.gateway_id, fixture.gatewayId);
  assert.deepEqual(registry.data.active_workspace, { gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId });
  assert.equal(workspace.data.workspace_id, fixture.workspaceId);
  const roots = (await api('/api/v1/workspace/files/roots')).data;
  // 五个普通文件和原生Settings组成六个真实编辑器，全部保留至采样结束。
  for (let index = 0; index < 5; index++) {
    const uri = await page.evaluate(({ owner, root, index }) => globalThis.workbench.URI.from({ scheme: 'boxteam-file',
      authority: owner.gatewayId, path: `/${owner.workspaceId}/${encodeURIComponent(root)}/latency-editor-${index}.txt` }).toString(),
    { owner: fixture, root: roots.workspace_uri, index });
    await command('_workbench.open', uri, [undefined, { pinned: true }]);
    await expect(page.locator('.editor-group-container.active .view-lines')).toContainText(`CONFIG LATENCY EDITOR ${index}`);
  }
  const location = { version: 2, gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId,
    source_key: 'workspace_root_mutable_override', name: '配置性能验收' };
  const source = (await api('/api/v1/config/sources/workspace_root_mutable_override')).data;
  const configured = source.source_effective.llm.providers;
  assert.equal(Object.keys(configured).length, 20);
  for (const value of Object.values(configured)) assert.equal(Object.keys(value.models).length, 10);
  result.owner = { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId, activeRevision: source.active_revision };
  firstSettingsStarted = performance.now();
  if (!fixture.httpOnly) await command('boxteam.settings.modelsProviders', location, fixture.providers[0], fixture.models[0]);
  const form = page.locator('.configuration-entities[data-definition-kind="providers"]');
  if (!fixture.httpOnly) {
    await expect(form.locator('article[data-definition-id]')).toHaveAttribute('data-definition-id', fixture.providers[0]);
    await expect(page.locator('.tabs-container .tab')).toHaveCount(6);
    result.firstSettingsShellMs = performance.now() - firstSettingsStarted;
  }
  if (!fixture.firstSettingsOnly) {
  result.phase = 'cold-inspection';
  const query = (provider, model) => '/api/v1/config/llm/inspection?' + new URLSearchParams({ provider_id: provider, model_id: model });
  const cold = await api(query(fixture.providers[0], fixture.models[0]));
  result.coldMs = cold.elapsedMs;
  assert.equal(cold.data.active_digest, source.active_digest);
  assert.equal(cold.data.active_revision, source.active_revision);
  assert(cold.data.model_options.chain.length >= 3);
  assert(cold.data.model_options.chain.some(node => node.definition_path === '/profiles/provider'));
  result.phase = 'warm-inspection';
  for (let iteration = 0; iteration < 20; iteration++) {
    const provider = fixture.providers[iteration], model = fixture.models[iteration % 10];
    const receipt = await api(query(provider, model));
    assert.equal(receipt.data.gateway_id, fixture.gatewayId);
    assert.equal(receipt.data.workspace_id, fixture.workspaceId);
    assert.equal(receipt.data.active_digest, cold.data.active_digest);
    assert.deepEqual(receipt.data.source_baseline, cold.data.source_baseline);
    assert.deepEqual(receipt.data.model_options.model_ref, { provider_id: provider, model_id: model });
    assert(receipt.bytes <= 512 * 1024);
  }
  result.warm = summary(result.httpSamples.filter(item => item.phase === 'warm-inspection').map(item => item.elapsedMs));
  }
  if (!fixture.httpOnly) {
    result.phase = 'model-settings';
    for (let iteration = 0; iteration < (fixture.firstSettingsOnly ? 1 : 20); iteration++) {
      const provider = fixture.providers[iteration], model = fixture.models[iteration % 10];
      const started = fixture.firstSettingsOnly ? firstSettingsStarted : performance.now();
      if (!fixture.firstSettingsOnly) await command('boxteam.settings.modelsProviders', location, provider, model);
      const article = form.locator(`article[data-definition-id="${provider}"]`), detail = article.locator(`[data-model-id="${model}"]`);
      await expect(detail).toBeVisible();
      await expect(detail.getByRole('textbox', { name: '模型名称', exact: true })).toBeEditable();
      await expect(detail.getByRole('textbox', { name: '模型名称', exact: true })).toHaveValue(`latency-upstream-${String(iteration % 10).padStart(2, '0')}`);
      await openDetails(detail.locator('.configuration-model-overrides'));
      const node = detail.locator('.configuration-node-reference');
      await openDetails(node);
      const response = page.waitForResponse(value => value.request().method() === 'GET'
        && new URL(value.url()).pathname === '/api/v1/config/llm/inspection'
        && new URL(value.url()).searchParams.get('provider_id') === provider
        && new URL(value.url()).searchParams.get('model_id') === model);
      await node.getByRole('button', { name: '预检并查看最终选项', exact: true }).click();
      const observed = await response, raw = await observed.text(), envelope = JSON.parse(raw);
      if (fixture.firstSettingsOnly) {
        const requestStart = requestStarts.get(observed.request());
        const sample = { elapsedMs: performance.now() - requestStart.started,
          wallStartedMs: requestStart.wallStartedMs, wallFinishedMs: Date.now(), bytes: Buffer.byteLength(raw),
          requestId: envelope.request_id, url: new URL(observed.url()).pathname, phase: 'cold-inspection' };
        result.httpSamples.push(sample);
        result.coldMs = sample.elapsedMs;
      }
      assert.equal(observed.status(), 200, JSON.stringify(envelope));
      assert.equal(envelope.request_id, observed.headers()['x-request-id']);
      assert.deepEqual(envelope.data.model_options.model_ref, { provider_id: provider, model_id: model });
      await expect(node.locator('[data-model-options="active"]')).toContainText('latency.invalid');
      await expect(detail.getByRole('textbox', { name: 'API 地址', exact: true })).toBeEditable();
      await expect(detail.getByRole('textbox', { name: 'API 地址', exact: true })).toHaveAttribute('placeholder', 'https://latency.invalid/v1');
      const elapsedMs = performance.now() - started;
      result.settingsMs.push(elapsedMs);
      if (fixture.firstSettingsOnly) result.firstSettingsOpenMs = elapsedMs;
      result.uiInspections.push({ provider, model, requestId: envelope.request_id, elapsedMs });
      await expect(page.locator('.tabs-container .tab')).toHaveCount(6);
      await observer.checkpoint('config.settings', iteration);
    }
    result.settings = summary(result.settingsMs);
  }
  result.domNodes = await page.locator('*').count();
  assert(result.coldMs <= 3000, `首次inspection ${result.coldMs}ms超过3000ms`);
  if (!fixture.firstSettingsOnly) assert(result.warm.p95 <= 500 && result.warm.max <= 1500, JSON.stringify(result.warm));
  if (!fixture.httpOnly) assert(result.settings.max <= 2000, `可操作模型设置超出2000ms: ${JSON.stringify(result.settings)}`);
  assert.deepEqual(result.pageErrors, []);
  assert.deepEqual(result.forbiddenExecutions, []);
  result.phase = fixture.httpOnly ? 'http-only-passed' : fixture.firstSettingsOnly ? 'first-settings-passed' : 'passed';
} catch (error) {
  failure = error;
  result.error = error.stack ?? String(error);
  if (page) await page.screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true }).catch(error => { result.screenshotError = String(error); });
} finally {
  for (const [label, run] of cleanup.reverse()) {
    try { await run(); } catch (error) { result.cleanupErrors ??= []; result.cleanupErrors.push({ label, error: String(error) }); failure ??= error; }
  }
  await writeFile(path.join(artifacts, 'latency.json'), JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;
