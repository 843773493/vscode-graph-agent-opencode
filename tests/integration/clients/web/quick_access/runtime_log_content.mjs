import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { searchReceipt } from '../../../../support/workbench/search-observation.mjs';
import { confirmContentCategories } from './default_interactions.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!base || !output || !screenshot || !process.env.BOXTEAM_LOG_WORKSPACE_B) throw new Error('运行日志验收缺少隔离目标或产物目录');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_runtime_log_content.py');
const QUERY = '运行日志验收😀词', GATEWAY_QUERY = 'Gateway日志验收😀词';
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], console: [], failedRequests: [], searchResponses: [], searches: [], receipts: [], locations: [], fullDiagnostics: [],
  boundary: '正式 Workbench／真实 Gateway 和受管 Backend；文件读取失败及原 DTO 身份拒绝生成真实进程日志；Gateway stderr 由测试宿主写入显式原诊断入口，不预写日志、搜索页或索引' };
const reads = [], failures = [];
let page;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1500, height: 1050 }, reducedMotion: 'reduce' });
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', message => result.console.push({ type: message.type(), text: message.text() }));
  page.on('requestfailed', request => result.failedRequests.push({ path: new URL(request.url()).pathname, error: request.failure()?.errorText }));
  page.on('response', response => {
    if (new URL(response.url()).pathname === '/api/gateway/search') result.searchResponses.push({ status: response.status(), requestId: response.headers()['x-request-id'] });
  });
  await page.addInitScript(() => {
    globalThis.runtimeLogQuickEvents = [];
    const describe = target => target instanceof Element ? `${target.localName}.${target.className}` : String(target);
    document.addEventListener('focusin', event => globalThis.runtimeLogQuickEvents.push({ event: 'focusin', target: describe(event.target), at: performance.now() }), true);
    new MutationObserver(records => {
      for (const record of records) if (record.target instanceof Element && record.target.classList.contains('quick-input-widget')) {
        globalThis.runtimeLogQuickEvents.push({ event: record.attributeName, value: record.target.getAttribute(record.attributeName),
          active: describe(document.activeElement), text: record.target.textContent.slice(-800), at: performance.now() });
      }
    }).observe(document, { attributes: true, subtree: true, attributeFilter: ['class', 'style'] });
  });
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/search') result.searches.push(request.postDataJSON());
    if (path === '/api/gateway/diagnostics/locate') result.locations.push(request.postDataJSON());
    if (path === '/api/gateway/diagnostics' || path === '/api/v1/search') result.fullDiagnostics.push(path);
  });
  page.on('requestfinished', request => {
    if (new URL(request.url()).pathname === '/api/gateway/search') reads.push(searchReceipt(request).then(receipt => result.receipts.push(receipt)));
  });
  const api = (path, body, owner, expected = 200) => page.evaluate(async ({ path, body, owner, expected }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: {
      'X-Local-Token': token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Workspace-Id': owner.workspace_id, 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id,
      } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json();
    if (response.status !== expected || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return expected === 200 ? payload.data : payload;
  }, { path, body, owner, expected });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health'), active = await api('/api/v1/workspace');
  const a = { gateway_id: health.gateway_id, workspace_id: active.workspace_id };
  await api('/api/gateway/managed-workspaces', { root_path: process.env.BOXTEAM_LOG_WORKSPACE_B, name: '日志隔离 B' });
  const workspaces = await api('/api/gateway/workspaces?check_health=false');
  const b = workspaces.items.find(item => item.name === '日志隔离 B');
  expect(b.backend_url).not.toBe(workspaces.items.find(item => item.workspace_id === a.workspace_id).backend_url);
  const owners = [a, { gateway_id: b.gateway_id, workspace_id: b.workspace_id }];
  const missing = (owner = a, suffix = 'A') => api('/api/v1/workspace/files/content?path=' + encodeURIComponent(`不存在-${QUERY}${suffix}--${QUERY}${suffix}.txt`), undefined, owner, 404);
  await missing();
  await missing(owners[1], 'B');
  await api('/api/gateway/diagnostics/query', { scope: { source: 'workspace', workspace_ids: [GATEWAY_QUERY] }, query: 'unused' }, undefined, 422);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const quick = page.locator('.quick-input-widget');
  const rows = () => quick.locator('.monaco-list-row').filter({ has: page.locator('[data-quick-input-id^="log:"]') });
  const aRows = () => quick.locator(`[data-quick-input-id^="log:${a.gateway_id}/workspace:${a.workspace_id}:workspace_api/"]`);
  const openSearch = async (query, category) => {
    await command('workbench.action.quickOpen', 'content ' + query);
    await quick.locator(`[data-quick-input-id="content-category-${category}"]`).click();
    await confirmContentCategories(quick, [category]);
    await expect(rows().first()).toBeVisible({ timeout: 15000 });
    await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
  };
  result.phase = 'explicit-category';
  await command('workbench.action.quickOpen', 'content ' + QUERY);
  await expect(quick.locator('[data-quick-input-id="content-category-runtime-logs"]')).toBeVisible();
  expect(result.searches).toHaveLength(0);
  result.unselectedHasNoQuery = true;
  await quick.locator('[data-quick-input-id="content-category-runtime-logs"]').click();
  await confirmContentCategories(quick, ['runtime-logs']);
  await expect(rows().first()).toBeVisible();
  await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
  await quick.locator('.monaco-list-row').filter({ hasText: b.workspace_id }).getByRole('checkbox').check();
  await quick.getByRole('button', { name: 'OK', exact: true }).click();
  await expect(rows().first()).toBeVisible();
  await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
  await Promise.all(reads);
  const both = result.receipts.find(receipt => receipt.scope.owners.length === 2);
  expect(both.summary.branches).toHaveLength(1);
  const logPage = both.pages[0].diagnostic_logs;
  expect(logPage.sources.filter(source => source.service === 'workspace_api')).toHaveLength(2);
  expect(new Set(logPage.items.flatMap(item => item.source.workspace_ids))).toEqual(new Set(owners.map(owner => owner.workspace_id)));
  expect(logPage.items.every(item => item.source.workspace_ids.length === 1)).toBe(true);
  expect(new Set(logPage.items.map(item => `${item.reference.log_id}/${item.reference.line_start}/${item.reference.start_codepoint}`)).size).toBe(logPage.items.length);
  const bOnly = await api('/api/gateway/diagnostics/query', { scope: { source: 'workspace', workspace_ids: [b.workspace_id] }, query: QUERY + 'A' });
  expect(bOnly.items).toHaveLength(0);
  expect(logPage.items.some(item => item.preview.includes(GATEWAY_QUERY))).toBe(false);
  result.workspaceSourcesIsolated = true;
  result.phase = 'precise-location';
  await aRows().first().click();
  const pane = page.getByLabel('运行日志原行定位'), mark = pane.locator('mark');
  await expect(mark).toHaveText(QUERY);
  const first = result.locations.at(-1);
  expect(first.scope.workspace_ids).toHaveLength(2);
  await expect(pane).toContainText('受管进程');
  await openSearch(QUERY, 'runtime-logs');
  await aRows().nth(1).click();
  await expect(mark).toHaveText(QUERY);
  const second = result.locations.at(-1);
  expect(second.reference.line_start).toBe(first.reference.line_start);
  expect(second.reference.start_codepoint).toBeGreaterThan(first.reference.start_codepoint);
  expect(await pane.count()).toBe(1);
  result.samePaneNewLocation = true;
  const scope = { source: 'workspace', workspace_ids: [a.workspace_id] };
  const pageBeforeAppend = await api('/api/gateway/diagnostics/query', { scope, query: QUERY, limit: 1 });
  expect(pageBeforeAppend.cursor).toBeTruthy();
  await missing();
  const unchanged = await api('/api/gateway/diagnostics/locate', first);
  expect(Array.from(unchanged.text).slice(first.reference.start_codepoint, first.reference.end_codepoint).join('')).toBe(QUERY);
  result.exactRecordAfterAppend = true;
  result.cursorDiagnostic = { scope: { mode: 'resume', scope: both.scope }, queries: [{ category: 'runtime_log', operation: 'content',
    match_mode: 'literal', query: QUERY, limit: 1, diagnostic_targets: [{ gateway_id: a.gateway_id, scope }] }],
    continuations: [{ branch: { category: 'runtime_log', operation: 'content', diagnostics: { gateway_id: a.gateway_id, scope } }, cursor: pageBeforeAppend.cursor }] };
  await pane.getByRole('button', { name: '关闭定位', exact: true }).click();
  result.phase = 'gateway-source';
  await openSearch(GATEWAY_QUERY, 'gateway-logs');
  await rows().first().locator('[data-quick-input-id]').click();
  await expect(mark).toHaveText(GATEWAY_QUERY);
  const gatewayLocation = result.locations.at(-1);
  expect(gatewayLocation.scope).toEqual({ source: 'gateway', workspace_ids: [] });
  await expect(pane).toContainText('Gateway 自有来源');
  result.gatewaySourceSeparate = true;
  await pane.getByRole('button', { name: '关闭定位', exact: true }).click();
  result.phase = 'combined-log-sources';
  const combinedQuery = '日志验收😀词', searchesBeforeCombined = result.searches.length;
  await command('workbench.action.quickOpen', 'content ' + combinedQuery);
  await quick.locator('[data-quick-input-id="content-category-runtime-logs"]').click();
  expect(result.searches).toHaveLength(searchesBeforeCombined);
  await confirmContentCategories(quick, ['runtime-logs', 'gateway-logs']);
  await expect(rows().first()).toBeVisible();
  await expect(quick.locator('.monaco-progress-container.active')).toHaveCount(0);
  await Promise.all(reads);
  expect(result.searches).toHaveLength(searchesBeforeCombined + 1);
  const combinedRequest = result.searches.at(-1), combinedWire = combinedRequest.queries[0];
  expect(combinedRequest.queries).toHaveLength(1);
  expect(combinedWire).toMatchObject({ category: 'runtime_log', operation: 'content', query: combinedQuery });
  expect(combinedWire.diagnostic_targets).toEqual([
    { gateway_id: a.gateway_id, scope: { source: 'workspace', workspace_ids: combinedRequest.scope.owners.map(owner => owner.workspace_id) } },
    { gateway_id: a.gateway_id, scope: { source: 'gateway', workspace_ids: [] } },
  ]);
  const combinedReceipt = result.receipts.find(receipt => receipt.pages.some(page => page.diagnostic_logs.query === combinedQuery));
  expect(combinedReceipt.summary.branches).toHaveLength(2);
  expect(new Set(combinedReceipt.pages.filter(page => page.diagnostic_logs.items.length > 0)
    .map(page => page.branch.branch.diagnostics.scope.source))).toEqual(new Set(['workspace', 'gateway']));
  result.combinedLogSourcesKeepIndependentBranches = true;
  await page.keyboard.press('Escape');
  result.phase = 'stale-reference';
  await page.route('**/api/gateway/diagnostics/locate', route => {
    const body = route.request().postDataJSON();
    return route.continue({ postData: JSON.stringify({ ...body, reference: { ...body.reference, content_version: '0'.repeat(64) } }) });
  });
  await openSearch(QUERY, 'runtime-logs');
  const rejected = page.waitForResponse(response => response.url().endsWith('/diagnostics/locate') && response.status() === 409);
  await rows().first().locator('[data-quick-input-id]').click();
  expect((await (await rejected).json()).detail).toMatchObject({ owner: 'runtime_log', code: 'stale_runtime_log_reference', reason: 'record_changed' });
  await expect(pane.getByRole('alert')).toContainText('原运行日志记录已改写');
  await expect(mark).toHaveCount(0);
  result.staleReferenceRejected = true;
  expect(result.fullDiagnostics).toEqual([]);
  result.noFullDiagnosticsRead = true;
  expect(result.searches.every(request => request.queries.length === 1 && request.queries[0].category === 'runtime_log'
    && request.queries[0].diagnostic_targets.length === (request === combinedRequest ? 2 : 1))).toBe(true);
  result.onlyFederatedSearch = true;
  result.phase = 'complete';
} catch (error) {
  failures.push(error); result.error = String(error?.stack ?? error);
  if (page) {
    result.quickEvents = await page.evaluate(() => globalThis.runtimeLogQuickEvents);
    const diagnostics = await Promise.allSettled([page.content().then(content => writeFile(screenshot + '.html', content)), page.screenshot({ path: screenshot, fullPage: true })]);
    failures.push(...diagnostics.filter(item => item.status === 'rejected').map(item => item.reason));
  }
} finally {
  const settled = await Promise.allSettled(reads);
  failures.push(...settled.filter(item => item.status === 'rejected').map(item => item.reason));
  try { await run.close(); } catch (error) { failures.push(error); }
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); } catch (error) { failures.push(error); }
}
if (failures.length) throw new AggregateError(failures, '运行日志正文验收失败');
