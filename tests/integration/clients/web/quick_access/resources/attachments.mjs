// Integration：真实 Thread 附件目录、原 Quick Access 与原附件预览；仅记录网络事实。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`缺少 ${name}`); return value; };
const fixture = JSON.parse(required('BOXTEAM_BROWSER_FIXTURE'));
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/test_attachments.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], attachmentPages: [], contentReads: [],
  workspaceContentReads: [], browseContentReads: [],
  boundary: '真实 Gateway／Backend 与正式 Workbench；消息通过产品 HTTP 入口上传，附件候选来自精确 Thread 分页，原文件读取仅在接受后由原预览执行' };
const pending = [], collectionErrors = [], pageRequests = new WeakMap();
let page, failure;
const phase = async value => { result.phase = value; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
const waitFor = async predicate => expect.poll(predicate, { timeout: 20_000 }).toBe(true);

try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Thread 附件 Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1700, height: 1100 } });
  run.addCleanup('Thread 附件 BrowserContext', () => context.close());
  page = await context.newPage(); page.setDefaultTimeout(20_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const url = new URL(request.url()), headers = request.headers();
    if (/^\/api\/v1\/sessions\/[^/]+\/threads\/[^/]+\/attachments$/.test(url.pathname)) {
      const observed = {
        phase: result.phase,
        path: url.pathname,
        method: request.method(),
        after: url.searchParams.get('after') ?? '',
        limit: url.searchParams.get('limit'),
        gatewayId: headers['x-boxteam-target-gateway-id'],
        workspaceId: headers['x-boxteam-workspace-id'],
      };
      result.attachmentPages.push(observed);
      pageRequests.set(request, observed);
    }
    if (/^\/api\/v1\/sessions\/[^/]+\/attachments\/content$/.test(url.pathname)) {
      const observed = { phase: result.phase, fileId: url.searchParams.get('file_id'), variant: url.searchParams.get('variant') };
      result.contentReads.push(observed);
      if (result.phase === 'browse' || result.phase === 'filter-cancel' || result.phase === 'pagination') result.browseContentReads.push(observed);
    }
    if (url.pathname === '/api/v1/workspace/files/content') result.workspaceContentReads.push({ phase: result.phase, path: url.pathname });
  });
  page.on('requestfinished', request => {
    const url = new URL(request.url());
    if (!/^\/api\/v1\/sessions\/[^/]+\/threads\/[^/]+\/attachments$/.test(url.pathname)) return;
    const observed = pageRequests.get(request);
    pending.push((async () => {
      const response = await request.response(), body = await response.json();
      if (!observed) throw new Error(`缺失附件分页请求记录：${url}`);
      Object.assign(observed, { status: response.status(), header: response.headers()['x-request-id'],
        request_id: body.request_id, data: body.data, detail: body.detail });
    })().catch(cause => collectionErrors.push(String(cause))));
  });

  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, owner, body) => page.evaluate(async ({ path, owner, body }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', body: body === undefined ? undefined : JSON.stringify(body),
      headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gateway_id, 'X-BoxTeam-Workspace-Id': owner.workspace_id } : {}) } });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, payload }));
    return payload.data;
  }, { path, owner, body });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const health = await api('/api/gateway/health'), registered = await api('/api/gateway/workspaces?check_health=false');
  expect(health.gateway_id).toBe(fixture.gatewayId);
  expect(registered.items).toContainEqual(expect.objectContaining({ gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId }));
  const owner = { gateway_id: fixture.gatewayId, workspace_id: fixture.workspaceId };
  expect((await api('/api/v1/workspace', owner)).workspace_id).toBe(fixture.workspaceId);
  const sessionButton = page.locator(`button[data-session-id="${fixture.sessionId}"]`);
  await expect(sessionButton).toBeVisible({ timeout: 30_000 });
  await sessionButton.dblclick();
  const threadSelector = `section.boxteam-thread[data-workspace-id="${fixture.workspaceId}"][data-session-id="${fixture.sessionId}"][data-thread-id="${fixture.threadId}"]`;
  const thread = page.locator(threadSelector);
  await expect(thread).toBeVisible();
  await expect(thread.locator('textarea[data-testid="composer-input"]')).toBeEnabled({ timeout: 30_000 });

  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const rows = name => quick.locator('.monaco-list-row').filter({ hasText: name });
  const openPicker = async () => {
    await command('boxteam.quickAccess.resources', { gatewayId: fixture.gatewayId, workspaceId: fixture.workspaceId });
    await expect(input).toHaveValue('resource ');
  };
  const filter = async name => {
    await input.fill(`resource ${name}`);
    await expect(rows(name)).toBeVisible();
  };
  const acceptCurrent = async (name, fileId, shortcut) => {
    await filter(name);
    await expect(rows(name)).toHaveCount(1);
    const item = rows(name).locator('[data-quick-input-id]');
    const expectedId = JSON.stringify([
      fixture.gatewayId, fixture.workspaceId, fixture.sessionId, fixture.threadId, 'attachment', fileId,
    ]);
    await expect(item).toHaveAttribute('data-quick-input-id', expectedId);
    const focused = quick.locator('.monaco-list-row.focused [data-quick-input-id]');
    const rowCount = await quick.locator('.monaco-list-row').count();
    for (let index = 0; index < rowCount; index += 1) {
      if (await focused.getAttribute('data-quick-input-id') === expectedId) break;
      await page.keyboard.press('ArrowDown');
    }
    await expect(focused).toHaveAttribute('data-quick-input-id', expectedId);
    if (shortcut) await page.keyboard.press(shortcut);
    else await page.keyboard.press('Enter');
  };
  const readRegion = name => page.locator('.editor-group-container.active').getByRole('region', { name: `附件预览：${name}` });

  await phase('browse');
  await openPicker();
  await filter(fixture.textName);
  await waitFor(() => result.attachmentPages.filter(item => item.phase === 'browse').length === 1);
  await waitFor(() => Boolean(result.attachmentPages.find(item => item.phase === 'browse')?.data));
  expect(result.contentReads).toEqual([]);
  result.browseOnly = true;

  await phase('filter-cancel');
  await input.fill('resource thread-quick-access-');
  await expect(quick).toContainText('thread-quick-access-');
  await page.keyboard.press('Escape');
  await expect(quick).toBeHidden();
  expect(result.contentReads).toEqual([]);
  result.filterAndCancel = true;

  await phase('pagination');
  await openPicker();
  await filter(fixture.textName);
  await waitFor(() => result.attachmentPages.filter(item => item.phase === 'pagination').length === 1);
  await waitFor(() => Boolean(result.attachmentPages.find(item => item.phase === 'pagination')?.data));
  const more = quick.locator('.monaco-list-row').filter({ hasText: '更多附件…' });
  await expect(more).toBeVisible();
  await more.hover();
  await more.getByRole('button', { name: '读取下一页附件', exact: true }).click();
  await waitFor(() => result.attachmentPages.filter(item => item.phase === 'pagination').length === 2);
  await waitFor(() => Boolean(result.attachmentPages.find(item => item.phase === 'pagination' && item.after)?.data));
  const paginatedItems = result.attachmentPages.filter(item => item.phase === 'pagination').flatMap(item => item.data.items);
  result.textFileId = paginatedItems.find(item => item.name === fixture.textName).file_id;
  result.mediaFileId = paginatedItems.find(item => item.name === fixture.mediaName).file_id;
  const nextPage = result.attachmentPages.find(item => item.phase === 'pagination' && item.after).data;
  expect(nextPage.items).toHaveLength(1);
  await filter(nextPage.items[0].name);
  expect(result.contentReads).toEqual([]);
  result.pagination = true;

  await phase('text-preview');
  await acceptCurrent(fixture.textName, result.textFileId);
  let preview = readRegion(fixture.textName);
  await expect(preview.locator('.workspace-attachment-text-preview pre')).toContainText(fixture.textContent);
  result.textPreview = true;

  await phase('media-preview');
  await openPicker();
  await input.fill(`resource ${fixture.mediaName}`);
  await waitFor(() => result.attachmentPages.some(item => item.phase === 'media-preview' && item.data));
  const firstMediaPage = result.attachmentPages.find(item => item.phase === 'media-preview');
  if (!firstMediaPage.data.items.some(item => item.name === fixture.mediaName)) {
    const mediaMore = quick.locator('.monaco-list-row').filter({ hasText: '更多附件…' });
    await mediaMore.hover();
    await mediaMore.getByRole('button', { name: '读取下一页附件', exact: true }).click();
    await waitFor(() => result.attachmentPages.some(item => item.phase === 'media-preview' && item.after && item.data));
  }
  await acceptCurrent(fixture.mediaName, result.mediaFileId, 'Alt+Enter');
  await expect(page.locator('.editor-group-container')).toHaveCount(2);
  await expect(page.locator('.editor-group-container.active').locator(threadSelector)).toBeVisible();
  preview = readRegion(fixture.mediaName);
  await expect(preview).toBeVisible();
  await expect(preview.locator('iframe[title^="PDF 附件："]')).toBeVisible();
  await expect(preview.getByRole('link', { name: '下载原件', exact: true })).toBeVisible();
  result.sideFocus = true;
  result.mediaPreview = true;

  await phase('restored');
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('.editor-group-container')).toHaveCount(2);
  const activeThread = page.locator('.editor-group-container.active').locator(threadSelector);
  await expect(activeThread).toBeVisible({ timeout: 30_000 });
  await expect(activeThread.getByRole('tab', { name: '文件', exact: true })).toHaveAttribute('aria-selected', 'true');
  preview = page.locator('.editor-group-container.active').getByRole('region', { name: `附件预览：${fixture.mediaName}` });
  await expect(preview.locator('iframe[title^="PDF 附件："]')).toBeVisible({ timeout: 30_000 });
  result.stateRestored = true;

  await Promise.all(pending);
  expect(collectionErrors).toEqual([]);
  expect(result.attachmentPages.every(item => item.method === 'GET' && item.limit === '100')).toBe(true);
  expect(result.attachmentPages.every(item => item.status === 200 && item.header && item.header === item.request_id)).toBe(true);
  expect(result.attachmentPages.every(item => item.data.session_id === fixture.sessionId && item.data.thread_id === fixture.threadId)).toBe(true);
  expect(result.attachmentPages.every(item => item.data.items.every(attachment => !('data_url' in attachment) && !('content' in attachment)))).toBe(true);
  expect(result.contentReads.every(item => item.variant === 'original')).toBe(true);
  expect(result.pageErrors).toEqual([]);
  await phase('complete');
} catch (cause) {
  failure = cause;
  result.error = String(cause.stack ?? cause);
} finally {
  if (page) await page.screenshot({ path: required('BOXTEAM_BROWSER_SCREENSHOT_PATH'), fullPage: false });
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
