import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../harness/js/run-context.mjs';
import { Perf } from '../../../harness/js/performance.mjs';
import { observePage } from '../../../harness/js/browser-performance.mjs';

const fixture = JSON.parse(process.env.BOXTEAM_MODEL_PICKER_FIXTURE);
assert(fixture.artifacts, '必须指定正式测试产物目录');
const scope = TestRunContext.fromTestFile('tests/integration/clients/web/test_model_picker.py');
const perf = new Perf(scope, { mode: 'core' });
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], requests: [], sessions: [], initialization: [],
  workload: { providers: 20, modelsPerProvider: 10, configurationLayers: 3, threadEditors: 6 },
  apiSamples: [], composerOpenMs: [], firstReads: fixture.firstReads,
  boundary: '真实 Gateway、Workspace 和 Workbench；延迟一次真实 Agent 回执，上游为统一协议资产的回环 HTTP 替身；正文到可见在同一浏览器时钟测量' };
let page, failure, headers, token;
async function api(url, init = {}) {
  const receipt = await page.evaluate(async ({ url, init, headers, token }) => {
    const started = performance.now();
    const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...headers } });
    const text = await response.text(), envelope = JSON.parse(text);
    if (!response.ok || envelope.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(envelope));
    return { data: envelope.data, requestId: envelope.request_id, elapsedMs: performance.now() - started,
      bytes: new TextEncoder().encode(text).length };
  }, { url, init, headers, token });
  const { data, ...sample } = receipt;
  result.apiSamples.push({ ...sample, path: url, phase: result.phase });
  perf.record('end', 'model.browser-api', { stage_id: crypto.randomUUID(), request_id: receipt.requestId,
    elapsed_ns: Math.round(receipt.elapsedMs * 1e6), response_bytes: receipt.bytes, status: 'passed', inclusive: true },
  { path: url, phase: result.phase });
  return { data, ...sample };
}
async function command(id, ...args) {
  return page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
}
async function open(session) {
  const started = performance.now();
  await command('_workbench.open', `boxteam-thread://${fixture.gatewayId}/${fixture.workspaceId}/${session.session_id}/${session.thread_id}`,
    [undefined, { pinned: true }]);
  const thread = page.locator(`.editor-group-container.active section.boxteam-thread[data-session-id="${session.session_id}"]`);
  await expect(thread.getByTestId('composer-input')).toBeEditable();
  result.composerOpenMs.push({ sessionId: session.session_id, elapsedMs: performance.now() - started });
  return thread;
}
const quick = () => page.locator('.quick-input-widget');
const modelLabels = { default: '日常助手', backup: '备用助手' };
const row = model => quick().locator('.monaco-list-row').filter({ has: page.locator('.label-name', { hasText: new RegExp(`^${modelLabels[model] ?? `picker-${model}`}$`) }) });
async function picker(thread) {
  await thread.locator('.composer-model-pill').click();
  await expect(quick()).toBeVisible();
  await expect(quick().locator('input').first()).toBeFocused();
  await expect(row('default')).toBeVisible();
  await expect(row('catalog-only')).toHaveCount(0);
}
async function choose(thread, model) {
  await picker(thread);
  await row(model).click();
  await expect(quick()).toBeHidden();
  await expect(thread.locator('.composer-model-label')).toHaveText(modelLabels[model]);
}
async function itemAction(model, name) {
  await row(model).hover();
  await row(model).getByRole('button', { name, exact: true }).click();
}
async function current(session) { return (await api(`/api/v1/sessions/${session.session_id}`)).data.current_model; }
const reference = model_id => ({ provider_id: 'primary', model_id });

async function verifyModelLayouts() {
  result.layouts = [];
  const form = page.locator('.editor-group-container.active .configuration-entities[data-definition-kind="providers"]');
  const inspect = async label => {
    const endpoint = form.getByLabel('API 地址').first();
    await expect(endpoint).toBeEditable();
    await endpoint.focus();
    const bounds = await endpoint.boundingBox();
    const content = await form.boundingBox();
    assert(bounds && content && bounds.width >= 120);
    assert(bounds.x >= content.x && bounds.x + bounds.width <= Math.min(content.x + content.width, page.viewportSize().width) + 1,
      `${label}: 地址输入框被裁切 ${JSON.stringify({ bounds, content })}`);
    const heading = form.getByRole('heading', { name: '模型与 Provider', exact: true });
    const titleBounds = await heading.boundingBox();
    assert(titleBounds && titleBounds.height <= 42, `${label}: 标题换行压住正文 ${JSON.stringify(titleBounds)}`);
    await expect(heading).toHaveAttribute('title', /^模型与 Provider\n/);
    await page.screenshot({ path: path.join(fixture.artifacts, `model-layout-${label}.png`), animations: 'disabled' });
    // 表单内部滚动后，底部动作仍能通过键盘到达；不发起额外探测。
    const action = form.getByRole('button', { name: '测试流式', exact: true });
    await action.focus();
    await expect(action).toBeInViewport();
    await expect(action).toBeFocused();
    const theme = await page.locator('.monaco-workbench').evaluate(node => node.classList.contains('vs-dark') ? 'dark' : 'light');
    result.layouts.push({ label, theme, viewport: page.viewportSize(), content, endpoint: bounds });
  };
  for (const label of ['theme-initial', 'theme-toggled']) {
    if (label === 'theme-toggled') await command('workbench.action.toggleLightDarkThemes');
    await inspect(label);
  }
  assert.equal(new Set(result.layouts.map(layout => layout.theme)).size, 2, '必须实际覆盖明暗两种主题');
  await command('workbench.action.toggleLightDarkThemes');
  await command('workbench.action.splitEditorRight');
  await expect(page.locator('.editor-group-container')).toHaveCount(2);
  await command('boxteam.settings.modelsProviders', { version: 2, gateway_id: fixture.gatewayId,
    workspace_id: fixture.workspaceId, source_key: 'workspace_root_mutable_override', name: '模型布局工作区' }, 'primary', 'backup');
  await expect(form).toBeVisible();
  for (const [label, x] of [['drag-left', 1], ['drag-right', 1399]]) {
    const group = await page.locator('.editor-group-container.active').boundingBox();
    assert(group);
    const sashes = page.locator('.part.editor .monaco-sash.vertical:visible');
    const boxes = await sashes.evaluateAll(nodes => nodes.map(node => {
      const { x, y, width, height } = node.getBoundingClientRect();
      return { x, y, width, height };
    }));
    const index = boxes.findIndex(box => box.height > 200 && Math.abs(box.x + box.width / 2 - group.x) < 8);
    assert(index >= 0, `未找到原生编辑器组分隔线: ${JSON.stringify({ group, boxes })}`);
    const sash = boxes[index];
    await page.mouse.move(sash.x + sash.width / 2, sash.y + sash.height / 2);
    await page.mouse.down();
    await page.mouse.move(x, sash.y + sash.height / 2, { steps: 15 });
    await page.mouse.up();
    await inspect(label);
  }
  assert(result.layouts.at(-1).content.width < result.layouts.at(-2).content.width, '两端拖拽必须实际改变设置容器宽度');
  await command('workbench.action.joinAllGroups');
  if (await page.locator('.part.sidebar').isVisible()) await command('workbench.action.toggleSidebarVisibility');
  await page.setViewportSize({ width: 460, height: 650 });
  await inspect('narrow-low-height');
  await page.setViewportSize({ width: 1400, height: 950 });
  await inspect('restored');
}

async function measureProbeBudget(panel) {
  // 只观察本场景的真实响应字节；不复制网络请求，不改正文、终态或取消语义。
  await panel.evaluate(panel => {
    const originalFetch = globalThis.fetch;
    const samples = [], pending = [];
    let scheduled = false;
    const rendered = () => {
      if (scheduled || !pending.length) return;
      scheduled = true;
      requestAnimationFrame(() => requestAnimationFrame(() => {
        scheduled = false;
        for (let index = pending.length - 1; index >= 0; index--) {
          const { sample, delta, prefix } = pending[index];
          const section = [...panel.querySelectorAll('.configuration-probe-result')].find(section =>
            [...section.querySelectorAll('small')].some(node => node.textContent === `request_id: ${sample.requestId}`));
          const text = section?.querySelector('pre'), bounds = text?.getBoundingClientRect();
          if (bounds?.width && bounds.height && text.checkVisibility() && text.textContent.startsWith(prefix)) {
            delta.visibleAt = performance.now();
            pending.splice(index, 1);
          }
        }
      }));
    };
    const observer = new MutationObserver(rendered);
    observer.observe(panel, { subtree: true, childList: true, characterData: true });
    globalThis.fetch = async (...args) => {
      const url = new URL(args[0] instanceof Request ? args[0].url : String(args[0]), location.href);
      if (!/^\/api\/v1\/config\/llm\/(discover|test)$/.test(url.pathname)) return originalFetch(...args);
      const sample = { path: url.pathname, startedAt: performance.now(), deltas: [] };
      samples.push(sample);
      const response = await originalFetch(...args);
      sample.headersAt = performance.now(); sample.requestId = response.headers.get('X-Request-ID');
      if (!response.ok || !sample.requestId || !response.body) throw new Error(`暖请求失败: ${response.status}`);
      const decoder = new TextDecoder();
      let buffer = '', prefix = '';
      const body = response.body.pipeThrough(new TransformStream({
        transform(bytes, controller) {
          if (url.pathname.endsWith('/test')) {
            buffer += decoder.decode(bytes, { stream: true });
            let boundary;
            while ((boundary = buffer.indexOf('\n\n')) !== -1) {
              const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
              if (!frame.startsWith('event: delta\n')) continue;
              const event = JSON.parse(frame.slice(frame.indexOf('data: ') + 6));
              prefix += event.text;
              const delta = { receivedAt: performance.now(), length: event.text.length };
              sample.deltas.push(delta); pending.push({ sample, delta, prefix });
            }
          }
          controller.enqueue(bytes);
        },
        flush() { sample.completedAt = performance.now(); },
      }));
      return new Response(body, { headers: response.headers, status: response.status, statusText: response.statusText });
    };
    globalThis.__modelProbeBudget = { samples, close() { globalThis.fetch = originalFetch; observer.disconnect(); } };
  });
  try {
    const button = panel.getByRole('button', { name: '测试流式', exact: true });
    // 首次读取单列且保留原样，随后二十个暖样本不删慢值。
    for (let index = 0; index < 21; index++) {
      for (const action of ['获取上游模型', '测试流式']) {
        const response = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(
          action === '获取上游模型' ? '/llm/discover' : '/llm/test'));
        await panel.getByRole('button', { name: action, exact: true }).click();
        const requestId = (await response).headers()['x-request-id'];
        await expect.poll(() => page.evaluate(requestId => globalThis.__modelProbeBudget.samples
          .some(sample => sample.requestId === requestId && sample.completedAt !== undefined), requestId)).toBe(true);
        if (action === '测试流式') {
          await expect(panel.locator('.configuration-probe-result').getByRole('status')).toContainText('本次通过');
          await expect.poll(() => page.evaluate(() => {
            const sample = globalThis.__modelProbeBudget.samples.at(-1);
            return sample.deltas.length > 0 && sample.deltas.every(delta => delta.visibleAt !== undefined);
          })).toBe(true);
        }
      }
      result.probeSamples = await page.evaluate(() => globalThis.__modelProbeBudget.samples);
      await writeFile(path.join(fixture.artifacts, 'probe-samples.json'), JSON.stringify(result.probeSamples, null, 2) + '\n');
    }
  } finally {
    result.probeSamples = await page.evaluate(() => {
      globalThis.__modelProbeBudget.close();
      return globalThis.__modelProbeBudget.samples;
    });
  }
  for (const suffix of ['discover', 'test']) {
    const samples = result.probeSamples.filter(sample => sample.path.endsWith('/' + suffix));
    assert.equal(samples.length, 21);
    const firstMs = samples[0].completedAt - samples[0].startedAt;
    assert(firstMs <= 3000, `首次 ${suffix} 请求耗时: ${firstMs}ms`);
    const durations = samples.slice(1).map(sample => sample.completedAt - sample.startedAt).sort((left, right) => left - right);
    result.apiBudgets['/api/v1/config/llm/' + suffix] = { count: 20, firstMs, p95: durations[18], max: durations[19] };
    for (const sample of samples) {
      perf.record('end', 'model.probe-warm', { stage_id: crypto.randomUUID(), request_id: sample.requestId,
        elapsed_ns: Math.round((sample.completedAt - sample.startedAt) * 1e6), status: 'passed', inclusive: true },
      { path: sample.path, external_wait: 'immediate_loopback_fixture' });
      for (const delta of sample.deltas) {
        const elapsed = delta.visibleAt - delta.receivedAt;
        assert(elapsed >= 0 && elapsed <= 250, `收到正文至可见: ${elapsed}ms, request_id=${sample.requestId}`);
        perf.record('metric', 'model.probe-content-visible', { request_id: sample.requestId,
          elapsed_ns: Math.round(elapsed * 1e6), characters: delta.length, boundary: 'same_browser_clock_dom_visible_after_paint' });
      }
    }
  }
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
  scope.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1400, height: 950 } });
  scope.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  const observer = await observePage(page, perf, context);
  if (observer.close) scope.addCleanup('页面性能观察', () => observer.close());
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    if (request.method() !== 'GET') result.requests.push({ path: new URL(request.url()).pathname, method: request.method() });
  });
  await page.goto(fixture.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#workbench .monaco-workbench')).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(() => Boolean(globalThis.workbench?.commands?.executeCommand));
  token = await page.evaluate(async () => (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token);
  const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
  const active = workspaces.data.active_workspace;
  fixture.gatewayId = active.gateway_id; fixture.workspaceId = active.workspace_id;
  headers = { 'X-BoxTeam-Target-Gateway-Id': active.gateway_id, 'X-BoxTeam-Workspace-Id': active.workspace_id };
  const workspace = await api('/api/v1/workspace');
  assert.equal(health.data.gateway_id, active.gateway_id);
  assert.equal(workspace.data.workspace_id, active.workspace_id);
  result.initialization = [health, workspaces, workspace].map(value => value.requestId);
  result.phase = 'six-sessions-through-product';
  for (let index = 0; index < 6; index++) {
    const created = await api('/api/v1/sessions', { method: 'POST', body: JSON.stringify({ title: `模型隔离 ${index}`, agent_id: 'default' }) });
    const main = await api(`/api/v1/sessions/${created.data.session_id}/main-thread`);
    result.sessions.push({ session_id: created.data.session_id, thread_id: main.data.thread_id });
  }
  for (const session of result.sessions) await open(session);
  let thread = await open(result.sessions[0]);
  result.phase = 'late-candidates-do-not-reopen-hidden-thread';
  let releaseCandidates, candidatesArrived = false;
  const released = new Promise(resolve => { releaseCandidates = resolve; });
  const delayCandidates = async route => {
    candidatesArrived = true;
    await released;
    await route.continue();
  };
  await page.route('**/api/v1/agents', delayCandidates, { times: 1 });
  try {
    await thread.locator('.composer-model-pill').click();
    result.phase = 'late-candidates-waiting-for-request';
    await expect.poll(() => candidatesArrived, { timeout: 10000 }).toBe(true);
    await expect(quick()).toBeVisible();
    result.phase = 'late-candidates-switch-thread';
    await open(result.sessions[1]);
    await expect(quick()).toBeHidden();
    const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/agents');
    releaseCandidates();
    await response;
    // 后续产品查询完成后再检查，避免在候选回调前得到假阳性。
    assert.deepEqual(await current(result.sessions[0]), reference('default'));
    await expect(quick()).toBeHidden();
    result.lateCandidatesSuppressed = true;
  } finally {
    releaseCandidates();
    await page.unrouteAll({ behavior: 'wait' });
  }
  thread = await open(result.sessions[0]);
  result.phase = 'display-name-and-server-name-search';
  await picker(thread);
  for (const query of ['备用助手', 'picker-backup', 'primary / backup']) {
    await quick().locator('input').first().fill(query);
    await expect(row('backup')).toBeVisible();
    await expect(row('default')).toHaveCount(0);
  }
  await page.keyboard.press('Escape');
  result.phase = 'favorite-and-hide-without-business-write';
  await picker(thread);
  const writes = result.requests.length;
  await itemAction('backup', '收藏模型');
  await itemAction('backup', '隐藏模型');
  await expect(row('backup')).toHaveCount(0);
  assert.equal(result.requests.length, writes);
  assert.deepEqual(await current(result.sessions[0]), reference('default'));
  await quick().getByRole('button', { name: '显示隐藏的模型', exact: true }).click();
  await itemAction('backup', '显示模型');
  await row('backup').click();
  await expect(thread.locator('.composer-model-label')).toHaveText('备用助手');
  assert.deepEqual(await current(result.sessions[0]), reference('backup'));
  await picker(thread);
  await itemAction('backup', '隐藏模型');
  await expect(row('backup')).toBeVisible();
  assert.deepEqual(await current(result.sessions[0]), reference('backup'));
  await page.keyboard.press('Escape');
  thread = await open(result.sessions[1]);
  await picker(thread);
  await expect(row('backup')).toHaveCount(0);
  await quick().getByRole('button', { name: '显示隐藏的模型', exact: true }).click();
  await itemAction('backup', '显示模型');
  await itemAction('backup', '取消收藏');
  await row('backup').click();
  await expect(thread.locator('.composer-model-label')).toHaveText('备用助手');
  await choose(thread, 'default');
  await picker(thread);
  await expect(quick()).toContainText('最近使用');
  await page.keyboard.press('Escape');
  result.phase = 'six-session-selection-isolation';
  const expected = ['backup', 'default', 'backup', 'default', 'backup', 'default'];
  for (let index = 2; index < 6; index++) {
    thread = await open(result.sessions[index]);
    await choose(thread, expected[index]);
  }
  for (let index = 0; index < 6; index++) assert.deepEqual(await current(result.sessions[index]), reference(expected[index]));
  result.phase = 'large-directory-warm-reads';
  result.apiBudgets = {};
  for (const url of ['/api/v1/agents', '/api/v1/config/sources', '/api/v1/config/sources/workspace_root_mutable_override']) {
    const samples = [];
    for (let index = 0; index < 20; index++) {
      const receipt = await perf.stage('model.warm-read', { path: url, sample: index }, () => api(url));
      if (url.endsWith('/workspace_root_mutable_override')) {
        const providers = Object.values(receipt.data.effective.llm.providers);
        assert.equal(providers.length, 20);
        assert(providers.every(provider => Object.keys(provider.models).length === 10));
        assert(receipt.data.layers.some(layer => layer.source_key === 'inline'));
        assert(receipt.data.layers.some(layer => layer.source_key === 'workspace_mutable_override'));
      }
      samples.push(receipt.elapsedMs);
      assert(receipt.bytes <= 512 * 1024, `${url}必须保留完整且有界的响应`);
    }
    samples.sort((left, right) => left - right);
    result.apiBudgets[url] = { count: samples.length, p95: samples[18], max: samples[19] };
  }
  result.phase = 'agent-candidates-stay-authoritative';
  await thread.locator('#agentSelectButton').click();
  await thread.getByRole('menuitemradio').filter({ hasText: 'reviewer' }).click();
  await expect(thread.locator('.composer-agent-label')).toHaveText('reviewer');
  await picker(thread);
  await expect(row('backup')).toHaveCount(0);
  await expect(row('default')).toBeVisible();
  await page.keyboard.press('Escape');
  result.phase = 'default-only-affects-new-sessions';
  thread = await open(result.sessions[0]);
  await picker(thread);
  await itemAction('backup', '设为工作区默认模型，仅影响新会话');
  await expect(quick()).toBeHidden();
  await expect.poll(async () => (await api('/api/v1/agents')).data.find(agent => agent.agent_id === 'default')
    .models.filter(model => model.workspace_default).map(model => model.model_ref)).toEqual([reference('backup')]);
  const newDefault = await api('/api/v1/sessions', { method: 'POST', body: JSON.stringify({ title: '新会话默认模型', agent_id: 'default' }) });
  assert.deepEqual(await current(newDefault.data), reference('backup'));
  result.newDefaultSession = newDefault.data.session_id;
  for (let index = 0; index < 6; index++) assert.deepEqual(await current(result.sessions[index]), reference(expected[index]));
  result.phase = 'native-storage-reload-and-model-settings';
  thread = await open(result.sessions[0]);
  await picker(thread);
  await itemAction('backup', '收藏模型');
  await page.keyboard.press('Escape');
  await command('boxteam.flushViewState');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => Boolean(globalThis.workbench?.commands?.executeCommand));
  thread = await open(result.sessions[0]);
  await picker(thread);
  await row('backup').hover();
  await expect(row('backup').getByRole('button', { name: '取消收藏', exact: true })).toBeVisible();
  const settingsStarted = performance.now();
  await itemAction('backup', '模型与 Provider');
  await expect(page.locator('.configuration-entities[data-definition-kind="providers"] article[data-definition-id="primary"] section[data-model-id="backup"]')).toBeVisible();
  await expect(page.locator('.configuration-entities[data-definition-kind="providers"]').getByLabel('API 地址').first()).toBeEditable();
  result.settingsOpenMs = performance.now() - settingsStarted;
  assert(!result.requests.some(request => /\/llm\/(?:test|discover)|\/messages$/.test(request.path)));
  result.phase = 'large-directory-probe-warm-budget';
  const panel = page.getByRole('region', { name: '发现与测试模型', exact: true });
  await expect(panel).toBeVisible();
  await measureProbeBudget(panel);
  result.phase = 'native-settings-narrow-and-extreme-drag';
  await verifyModelLayouts();
  assert.deepEqual(result.pageErrors, []);
  for (const [url, budget] of Object.entries(result.apiBudgets)) {
    assert(budget.p95 <= 500 && budget.max <= 1500, `${url}: ${JSON.stringify(budget)}`);
  }
  assert(result.composerOpenMs.every(sample => sample.elapsedMs <= 2000), JSON.stringify(result.composerOpenMs));
  assert(result.settingsOpenMs <= 2000, `模型设置可编辑耗时: ${result.settingsOpenMs}`);
  result.phase = 'complete';
} catch (error) { failure = error; result.failure = String(error.stack ?? error); }
finally {
  const errors = failure ? [failure] : [];
  if (failure && page) {
    try { await page.screenshot({ path: path.join(fixture.artifacts, 'failure.png'), fullPage: true }); }
    catch (error) { errors.push(error); }
  }
  try { await writeFile(path.join(fixture.artifacts, 'result.json'), JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { errors.push(error); }
  try { await scope.close(); } catch (error) { errors.push(error); }
  if (errors.length) throw new AggregateError(errors, '原生模型选择验收失败');
}
