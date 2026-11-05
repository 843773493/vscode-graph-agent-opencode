import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`Composer资源动作缺少 ${name}`);
  return value;
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_composer_resource_actions.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], executions: [], reads: [], addresses: [], receipts: [],
  boundary: '真实Gateway、双工作区、正式Workbench与原Composer/IndexedDB；产品入口创建Session，未预写草稿、历史或替换响应；model_stream仅用于后端启动' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('composer-browser', () => browser.close());
  page = await browser.newPage({ viewport: { width: 2100, height: 1050 } });
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path.endsWith('/messages')) result.executions.push(path);
    if (request.method() === 'POST' && path === '/api/gateway/resource-addresses/parse') result.addresses.push({ phase: result.phase, body: request.postDataJSON() });
    if (request.method() === 'GET' && /\/workspace\/files\/(raw|content)$/.test(path)) result.reads.push({ phase: result.phase, path, url: request.url() });
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = async (path, init = {}) => {
    const response = await page.evaluate(async ({ path, init }) => {
      const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...init.headers } });
      const body = await response.json(), header = response.headers.get('X-Request-ID');
      if (!response.ok || body.code !== 0 || !body.request_id || body.request_id !== header) throw new Error(JSON.stringify({ path, status: response.status, body }));
      return { data: body.data, receipt: { path, status: response.status, requestId: body.request_id, header } };
    }, { path, init });
    result.receipts.push(response.receipt);
    return response.data;
  };
  const headers = target => ({ 'X-BoxTeam-Target-Gateway-Id': target.gateway_id, 'X-BoxTeam-Workspace-Id': target.workspace_id });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  const receiverOwner = listing.active_workspace;
  expect(receiverOwner.gateway_id).toBe(health.gateway_id);
  expect((await api('/api/v1/workspace', { headers: headers(receiverOwner) })).workspace_id).toBe(receiverOwner.workspace_id);
  const registered = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({
    root_path: required('BOXTEAM_COMPOSER_OTHER_WORKSPACE'), name: 'Composer source B',
  }) });
  const sourceOwner = registered.items.find(item => item.name === 'Composer source B');
  expect(sourceOwner).toBeTruthy();
  expect(sourceOwner.workspace_id).not.toBe(receiverOwner.workspace_id);
  expect((await api('/api/v1/workspace', { headers: headers(sourceOwner) })).workspace_id).toBe(sourceOwner.workspace_id);
  const roots = await api('/api/v1/workspace/files/roots', { headers: headers(sourceOwner) });
  const source = roots.workspace_uri + '/selection.txt';
  const address = await api('/api/gateway/resource-addresses/parse', { method: 'POST', body: JSON.stringify({ uri: source, local_gateway_id: health.gateway_id }) });
  const file = await api(`/api/v1/workspace/files/content?path=${encodeURIComponent(source)}`, { headers: headers(sourceOwner) });
  expect(file.content).toBe('原选定正文😀\n下一行');
  const sessions = [];
  for (const title of ['接收者 A1', '接收者 A2']) {
    const session = await api('/api/v1/sessions', { method: 'POST', headers: headers(receiverOwner), body: JSON.stringify({ title }) });
    const thread = await api(`/api/v1/sessions/${session.session_id}/main-thread`, { headers: headers(receiverOwner) });
    sessions.push({ ...session, threadId: thread.thread_id });
  }
  result.sourceOwner = sourceOwner; result.receiverOwner = receiverOwner; result.sessions = sessions;
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const threadUri = session => `boxteam://${receiverOwner.gateway_id}/workspace/${receiverOwner.workspace_id}/resources/session/${session.session_id}/threads/${session.threadId}`;
  const composer = index => page.locator(`section.boxteam-thread[data-session-id="${sessions[index].session_id}"][data-thread-id="${sessions[index].threadId}"]`).locator('textarea[data-testid="composer-input"]').filter({ visible: true });
  await command('_workbench.open', threadUri(sessions[0]));
  await expect(composer(0)).toBeEnabled({ timeout: 30_000 });
  await composer(0).fill('A1 原草稿');
  await command('workbench.action.splitEditorRight');
  await command('_workbench.open', threadUri(sessions[1]));
  await expect(composer(1)).toBeEnabled({ timeout: 30_000 });
  await composer(1).fill('A2 原草稿');
  await command('workbench.action.splitEditorRight');
  await command('_workbench.open', source);
  const text = page.locator('.editor-instance .monaco-editor').filter({ visible: true });
  await expect(text.locator('.view-lines')).toContainText('原选定正文');
  const quick = page.locator('.quick-input-widget');
  result.phase = 'cancel-reference';
  let pending = command('boxteam.resource.attachToChat');
  await expect(quick).toContainText('选择接收草稿的 Thread');
  await expect(quick.getByRole('option')).toHaveCount(2);
  await page.keyboard.press('Escape');
  await pending;
  await expect(composer(0)).toHaveValue('A1 原草稿');
  await expect(composer(1)).toHaveValue('A2 原草稿');
  expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
  result.cancelUnchanged = true;

  result.phase = 'attach-reference';
  pending = command('boxteam.resource.attachToChat');
  await expect(quick).toContainText('选择接收草稿的 Thread');
  await quick.getByRole('option').filter({ hasText: sessions[0].threadId }).click();
  await pending;
  let expectedA1 = `A1 原草稿\n${address.reference_uri}`;
  await expect(composer(0)).toHaveValue(expectedA1);
  await expect(composer(1)).toHaveValue('A2 原草稿');
  expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
  expect(result.addresses.filter(read => read.phase === result.phase)).toEqual([{ phase: result.phase,
    body: { uri: source, local_gateway_id: health.gateway_id, resource_gateway_id: sourceOwner.gateway_id } }]);
  result.referenceRecipient = true;

  result.phase = 'originating-thread-keeps-recipient';
  await command('workbench.action.focusFirstEditorGroup');
  const internalSource = await page.evaluate(({ owner, root }) => globalThis.workbench.URI.from({
    scheme: 'boxteam-file', authority: owner.gateway_id,
    path: `/${owner.workspace_id}/${encodeURIComponent(root)}/selection.txt`,
  }).toString(), { owner: sourceOwner, root: roots.workspace_uri });
  let releaseResponse, held = false;
  const barrier = new Promise(resolve => { releaseResponse = resolve; });
  const delay = async route => {
    // 唯一具名故障边界：只延迟真实地址响应，不替换身份、状态或正文。
    const response = await route.fetch();
    held = true;
    await barrier;
    await route.fulfill({ response });
  };
  await page.route('**/api/gateway/resource-addresses/parse', delay);
  try {
    pending = page.evaluate(uri => globalThis.workbench.commands.executeCommand('boxteam.resource.attachToChat', globalThis.workbench.URI.parse(uri)), internalSource);
    await expect.poll(() => held).toBe(true);
    await command('workbench.action.focusSecondEditorGroup');
    await expect(quick).not.toBeVisible();
    releaseResponse();
    await pending;
  } finally {
    releaseResponse();
    await page.unroute('**/api/gateway/resource-addresses/parse', delay);
  }
  expectedA1 += `\n${address.reference_uri}`;
  await expect(composer(0)).toHaveValue(expectedA1);
  await expect(composer(1)).toHaveValue('A2 原草稿');
  expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
  result.originatingThreadKeptRecipient = true;

  result.phase = 'paste-selection';
  await text.getByRole('textbox').focus();
  await page.keyboard.press('ControlOrMeta+A');
  pending = command('boxteam.resource.pasteSelectionToChat');
  await expect(quick).toContainText('选择接收草稿的 Thread');
  await quick.getByRole('option').filter({ hasText: sessions[1].threadId }).click();
  await pending;
  const expectedA2 = 'A2 原草稿\n原选定正文😀\n下一行';
  await expect(composer(1)).toHaveValue(expectedA2);
  await expect(composer(0)).toHaveValue(expectedA1);
  expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
  result.pasteRecipient = true;

  result.phase = 'change-source';
  await api(`/api/v1/workspace/files/content?path=${encodeURIComponent(source)}`, { method: 'PUT', headers: headers(sourceOwner),
    body: JSON.stringify({ content: '来源后续正文', expected_revision: file.revision }) });
  await expect(composer(1)).toHaveValue(expectedA2);
  result.sourceChangePreservesPaste = true;
  result.phase = 'restore';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(composer(0)).toHaveValue(expectedA1, { timeout: 30_000 });
  await expect(composer(1)).toHaveValue(expectedA2, { timeout: 30_000 });
  expect(result.executions).toEqual([]);
  expect(result.pageErrors).toEqual([]);
  result.restoredDrafts = true;
  result.phase = 'complete';
} catch (error) {
  failure = error; result.failure = String(error);
  if (page) await page.screenshot({ path: required('BOXTEAM_BROWSER_SCREENSHOT_PATH'), fullPage: true }).catch(cause => { result.screenshotFailure = String(cause); });
} finally {
  try { await run.close(); } catch (error) { failure ??= error; result.cleanupFailure = String(error); }
  await writeFile(output, JSON.stringify(result, null, 2));
}
if (failure) throw failure;
