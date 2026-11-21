import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from '../../../../../src/shared/sse.js';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!baseUrl || !output) throw new Error('搜索诊断缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_search_diagnostics.py');
const result = { phase: 'initialize', pageErrors: [], initialization: [], searches: [], gatewayFailures: {},
  boundary: '真实Gateway、Workspace文件owner和正式Workbench，无业务HTTP替身' };
let failure, page;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  page = await browser.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('response', response => {
    if (new URL(response.url()).pathname !== '/api/gateway/search') return;
    result.searches.push({ request: response.request().postDataJSON(), status: response.status(), requestId: response.headers()['x-request-id'] });
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId("workspace-navigation-root-drop-target")).toBeVisible({ timeout: 30_000 });
  const api = async (path, init = {}) => page.evaluate(async ({ path, init }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return body;
  }, { path, init });
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    const body = await api(path); result.initialization.push({ path, requestId: body.request_id });
  }
  result.phase = 'gateway-failure-diagnostics';
  const active = (await api('/api/gateway/workspaces')).data.active_workspace;
  const owner = { gateway_id: active.gateway_id, workspace_id: active.workspace_id };
  const roots = await api('/api/v1/workspace/files/roots', { headers: {
    'X-BoxTeam-Workspace-Id': owner.workspace_id, 'X-BoxTeam-Target-Gateway-Id': owner.gateway_id,
  } });
  const emptyDirectory = `${roots.data.workspace_uri}/empty-search-directory`;
  const federatedSearch = async (requestId, contractVersion, query, matchMode, isRegex) => {
    const response = await page.evaluate(async ({ requestId, payload }) => {
      const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
      const response = await fetch('/api/gateway/search', { method: 'POST', headers: {
        'X-Local-Token': credential.data.token, 'X-Request-ID': requestId, 'Content-Type': 'application/json',
      }, body: JSON.stringify(payload) });
      return { status: response.status, responseRequestId: response.headers.get('X-Request-ID'),
        contentType: response.headers.get('Content-Type'), body: await response.text() };
    }, { requestId, payload: {
      scope: { mode: 'selected', owners: [owner] },
      queries: [{ category: 'file', operation: 'content', contract_version: contractVersion, query, match_mode: matchMode,
        limit: 1, file_targets: [{ owner, target: { uri: emptyDirectory, search_options: { is_regex: isRegex } } }], diagnostic_targets: [] }],
      continuations: [], max_results: 1,
    } });
    const events = [];
    if (response.status !== 200 || response.responseRequestId !== requestId) {
      throw new Error(JSON.stringify({ status: response.status, responseRequestId: response.responseRequestId, requestId }));
    }
    await consumeSseResponse(new Response(response.body, { headers: { 'Content-Type': response.contentType } }), {
      events: { search: defineSseEvent(decodeJsonSseData, event => events.push(event)) },
    });
    if (events.some(event => event.request_id !== requestId)) {
      throw new Error(JSON.stringify({ status: response.status, responseRequestId: response.responseRequestId, requestId, events }));
    }
    return { status: response.status, requestId, responseRequestId: response.responseRequestId, events };
  };
  const invalidRegex = await federatedSearch('search-diagnostics-empty-regex', 1, '[', 'regex', true);
  const invalidRegexBranch = invalidRegex.events.at(-1)?.summary?.branches?.[0];
  expect(invalidRegexBranch).toMatchObject({ state: 'failed', code: 'execution_failed', error_owner: 'resource_file_operations',
    reason: 'invalid_search_pattern', http_status: 400, cursor: null, returned_results: 0, retryable: false });
  expect(invalidRegex.events.some(event => event.type === 'page')).toBe(false);
  const unsupportedVersion = await federatedSearch('search-diagnostics-unsupported-version', 2, 'needle', 'literal', false);
  const unsupportedVersionBranch = unsupportedVersion.events.at(-1)?.summary?.branches?.[0];
  expect(unsupportedVersionBranch).toMatchObject({ state: 'failed', code: 'unsupported_version', http_status: 400,
    cursor: null, returned_results: 0, retryable: false });
  expect(unsupportedVersion.events.some(event => event.type === 'page')).toBe(false);
  result.gatewayFailures = {
    emptyDirectoryInvalidRegex: { requestId: invalidRegex.requestId, responseRequestId: invalidRegex.responseRequestId,
      branch: invalidRegexBranch, pageCount: invalidRegex.events.filter(event => event.type === 'page').length },
    unsupportedCapabilityVersion: { requestId: unsupportedVersion.requestId, responseRequestId: unsupportedVersion.responseRequestId,
      branch: unsupportedVersionBranch, pageCount: unsupportedVersion.events.filter(event => event.type === 'page').length },
  };
  const session = (await api('/api/v1/sessions', { method: 'POST', body: JSON.stringify({ title: '普通搜索诊断' }) })).data;
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  await command('_workbench.open', session.uri);
  await writeWorkspaceSettings({ page, command }, { 'search.maxResults': 1, 'files.autoSave': 'off' });
  result.phase = 'quick-info-and-limit';
  await command('workbench.action.quickOpen', '%noticeword');
  const quick = page.locator('.quick-input-widget');
  await expect(quick).toContainText('code: match_preview_truncated');
  await expect(quick.locator('.codicon-info')).toBeVisible();
  await expect(quick).toContainText('本次查询覆盖受限');
  await expect(quick.locator('.codicon-warning')).toBeVisible();
  result.quickInfo = true; result.quickLimited = true;
  const quickReads = result.searches.filter(item => item.request.queries.some(query => query.query === 'noticeword'));
  expect(quickReads).toHaveLength(1);
  await page.keyboard.press('Escape');
  await writeWorkspaceSettings({ page, command }, { 'search.maxResults': 10, 'files.autoSave': 'off' });
  result.phase = 'editor-info-and-warning';
  await command('search.action.openEditor', { query: 'noticeword', location: 'new', triggerSearch: true,
    contextLines: 1, useExcludeSettingsAndIgnoreFiles: false, onlyOpenEditors: false });
  const messages = page.locator('.search-editor .providerMessage');
  await expect(messages.filter({ hasText: 'code: match_preview_truncated' })).toBeVisible();
  await expect(messages.filter({ hasText: 'code: context_line_truncated' })).toBeVisible();
  await expect(messages.locator('a')).toHaveCount(0);
  result.editorWarning = true;
  const noticeSearches = result.searches.filter(item => item.request.queries.some(query => query.query === 'noticeword'));
  expect(noticeSearches).toHaveLength(2);
  expect(noticeSearches.every(item => item.status === 200 && item.requestId)).toBe(true);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error.stack ?? String(error);
} finally {
  if (page && failure) {
    try { await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true }); }
    catch (error) { result.screenshotError = String(error); }
  }
  result.phase = failure ? 'failed' : result.phase;
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { failure ??= error; result.writeError = String(error?.stack ?? error); }
  finally { await run.close(); }
}
if (failure) throw failure;
