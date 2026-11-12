import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
if (!baseUrl || !output) throw new Error('搜索诊断缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_search_diagnostics.py');
const result = { phase: 'initialize', pageErrors: [], initialization: [], searches: [],
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
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  const api = async (path, init = {}) => page.evaluate(async ({ path, init }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify(body));
    return body;
  }, { path, init });
  for (const path of ['/api/gateway/health', '/api/gateway/workspaces', '/api/v1/workspace']) {
    const body = await api(path); result.initialization.push({ path, requestId: body.request_id });
  }
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
  expect(result.searches).toHaveLength(2);
  expect(result.searches.every(item => item.status === 200 && item.requestId)).toBe(true);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error.stack ?? String(error);
  if (page) await page.screenshot({ path: output.replace(/\.json$/, '.png'), fullPage: true });
} finally {
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
