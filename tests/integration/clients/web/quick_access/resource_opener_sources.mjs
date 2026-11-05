import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
const expectedWorkspaceId = process.env.BOXTEAM_BROWSER_WORKSPACE_ID;
if (!base || !output || !screenshot || !expectedWorkspaceId) throw new Error('规范来源验收缺少隔离目标、预期工作区身份或产物目录');
const requestOwner = request => ({ gatewayId: request.headers()['x-boxteam-target-gateway-id'],
  workspaceId: request.headers()['x-boxteam-workspace-id'] });
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_resource_opener_sources.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], sources: [], links: [], addressRequests: [], fileReads: [], executions: [],
  boundary: '真实Gateway／后端／正式Workbench；model_stream精确回放；产品执行产生工具结果及规范引用，不预写历史、浏览器存储或HTTP响应' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1050 }, permissions: ['clipboard-read', 'clipboard-write'] });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/resource-addresses/parse') result.addressRequests.push({ phase: result.phase, body: request.postDataJSON() });
    if (path === '/api/v1/resources/navigation' && request.postDataJSON()?.operation === 'read') {
      result.sources.push({ phase: result.phase, body: request.postDataJSON(), ...requestOwner(request) });
    }
    if (/\/workspace\/files\/(raw|content)$/.test(path)) result.fileReads.push({ phase: result.phase, path });
    if (path.endsWith('/messages') && request.method() === 'POST') result.executions.push({ phase: result.phase, path, ...requestOwner(request) });
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const api = (path, data, owner) => page.evaluate(async ({ path, data, owner }) => {
    if (path.startsWith('/api/v1/') && !owner) throw new Error(`工作区请求缺少完整 owner：${path}`);
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': token, 'Content-Type': 'application/json', ...(owner ? {
        'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
      } : {}) }, body: data === undefined ? undefined : JSON.stringify(data) });
    const payload = await response.json();
    if (!response.ok || payload.code !== 0 || payload.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, payload }));
    }
    return payload.data;
  }, { path, data, owner });
  await api('/api/gateway/users/guest', { tracking: { source: 'resource-opener-sources' } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces?check_health=false');
  const workspaceOwner = { gatewayId: health.gateway_id, workspaceId: expectedWorkspaceId };
  const activeWorkspace = { gateway_id: workspaceOwner.gatewayId, workspace_id: workspaceOwner.workspaceId };
  expect(health.gateway_id).toBeTruthy();
  expect(health.active_workspace).toEqual(activeWorkspace);
  expect(targets.active_workspace).toEqual(activeWorkspace);
  expect(targets.items.filter(item => item.gateway_id === workspaceOwner.gatewayId && item.workspace_id === workspaceOwner.workspaceId)).toHaveLength(1);
  const workspace = await api('/api/v1/workspace', undefined, workspaceOwner);
  expect(workspace.workspace_id).toBe(workspaceOwner.workspaceId);
  const session = await api('/api/v1/sessions', { title: '规范工具结果来源验收' }, workspaceOwner);
  expect(session.workspace_id).toBe(workspaceOwner.workspaceId);
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, undefined, workspaceOwner);
  expect(main.session_id).toBe(session.session_id);
  const owner = { ...workspaceOwner, sessionId: session.session_id, threadId: main.thread_id };
  result.owner = owner;
  result.phase = 'execute-output';
  await testControl({ action: 'plan_model', session_id: owner.sessionId, thread_id: owner.threadId,
    interactions: JSON.parse(process.env.BOXTEAM_SOURCE_INTERACTIONS) });
  const accepted = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`,
    { message: { content: '生成完整工具结果' }, run: {} }, owner);
  expect(accepted.thread_id).toBe(owner.threadId);
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`, undefined, owner);
    expect(job.session_id).toBe(owner.sessionId);
    expect(job.thread_id).toBe(owner.threadId);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 30_000 }).toBe('completed');
  const history = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`,
    { turn_ids: [accepted.turn_id], include: ['tool_result'] }, owner);
  const tool = history.items.flatMap(turn => turn.response_parts).find(part => part.kind === 'tool_result'
    && part.result?.includes('/tool-results/'));
  // 使用原执行历史真正返回的引用，不按 Session 或工具调用 ID 拼造 URI。
  const toolUri = tool?.result.match(/boxteam:\/\/[^\s]+\/tool-results\/tool_[0-9a-f]{24}\.txt/)?.[0];
  expect(toolUri).toBeTruthy();
  result.tool = { uri: toolUri, turnId: accepted.turn_id, partId: tool.part_id };
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const quick = page.locator('.quick-input-widget'), groups = page.locator('.editor-group-container');
  const text = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true });
  const open = async (uri, modifiers = []) => {
    await command('workbench.action.quickOpen', uri);
    await expect(quick.locator('input')).toHaveValue(uri);
    const pick = quick.locator(`[data-quick-input-id="link:${uri}"]`);
    await expect(pick).toBeVisible();
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/resource-addresses/parse'
        && response.request().postDataJSON()?.uri === uri),
      pick.click({ modifiers }),
    ]);
    const body = await response.json();
    expect(response.status()).toBe(200);
    expect(body.request_id).toBe(response.headers()['x-request-id']);
    expect(body.data).toMatchObject({ scope: 'workspace', scope_id: owner.workspaceId, gateway_id: owner.gatewayId });
    result.links.push({ uri, address: body.data, requestId: body.request_id });
    return body.data;
  };
  await command('workbench.action.closeAllEditors');
  result.phase = 'baseline-source';
  await open(`boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/agent-spec/root/AGENTS.md`);
  await expect(text(0).locator('.view-lines')).toContainText('资源打开归属基线');
  result.phase = 'copy-source-reference';
  await command('workbench.action.showCommands');
  await quick.locator('input').fill('>复制资源 URI');
  const copyCommand = quick.locator('.monaco-list-row').filter({ hasText: '复制资源 URI' });
  await expect(copyCommand).toHaveCount(1);
  await copyCommand.click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(
    `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/agent-spec/root/AGENTS.md`);
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
  expect(result.fileReads.filter(read => read.phase === result.phase)).toEqual([]);
  await expect(groups).toHaveCount(1);
  result.sourceReferenceCopied = true;
  result.phase = 'canonical-tool-result';
  const address = await open(toolUri, ['Alt']);
  expect(address).toMatchObject({ kind: 'session', session_id: null });
  await expect(groups).toHaveCount(2);
  await expect(text(0).locator('.view-lines')).toContainText('资源打开归属基线');
  await expect(text(1).locator('.view-lines')).toContainText('large-output-line-0000');
  await expect(page.locator('section.boxteam-thread')).toHaveCount(0);
  await expect(groups.nth(1).locator('.tab.active')).toHaveAttribute('data-resource-name', /^tool_[0-9a-f]{24}\.txt$/);
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([
    { phase: result.phase, gatewayId: owner.gatewayId, workspaceId: owner.workspaceId,
      body: { uri: address.uri, operation: 'read', read_format: 'auto',
      max_bytes: 33554432, max_response_bytes: 50331648 } },
  ]);
  result.toolArtifactOpened = true;
  result.phase = 'copy-tool-reference';
  await command('boxteam.resource.copyUri');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(address.reference_uri);
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
  expect(result.fileReads.filter(read => read.phase === result.phase)).toEqual([]);
  await expect(groups).toHaveCount(2);
  result.toolReferenceCopied = true;
  // 默认截断预览不含第 1201 行，原 Go to Line 必须到达完整正文中间。
  await command('workbench.action.gotoLine');
  await expect(quick.locator('input')).toHaveValue(/^:/);
  await quick.locator('input').fill(':1201'); await page.keyboard.press('Enter');
  await expect(page.locator('[id="status.editor.selection"]')).toHaveText('Ln 1201, Col 1');
  await expect(text(1).locator('.view-lines')).toContainText('BOXTEAM_MIDDLE_SECRET_7F3A9C');
  await expect(text(1).getByRole('textbox')).toHaveAttribute('aria-autocomplete', 'none');
  await text(1).getByRole('textbox').focus(); await page.keyboard.insertText('UNEXPECTED TOOL MUTATION');
  await expect(text(1).locator('.view-lines')).not.toContainText('UNEXPECTED TOOL MUTATION');
  await expect(groups.nth(1).locator('.tab.active')).not.toHaveClass(/dirty/);
  result.toolArtifactReadonly = true;
  result.phase = 'split-tool-result';
  await command('workbench.action.splitEditorRight');
  await expect(groups).toHaveCount(3);
  await expect(text(2).locator('.view-lines')).toContainText('BOXTEAM_MIDDLE_SECRET_7F3A9C');
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
  result.toolArtifactSplitShared = true;
  result.phase = 'tool-result-refresh-restore';
  // 原正常 Reload Window 会等待生命周期提交；浏览器强制导航不保证异步存储完成。
  await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), command('workbench.action.reloadWindow')]);
  await expect(groups).toHaveCount(3);
  await expect(text(0).locator('.view-lines')).toContainText('资源打开归属基线');
  for (const index of [1, 2]) await expect(text(index).locator('.view-lines')).toContainText('BOXTEAM_MIDDLE_SECRET_7F3A9C');
  expect(result.sources.filter(read => read.phase === result.phase && read.body.uri === address.uri)).toHaveLength(1);
  result.toolArtifactRestored = true;
  await command('workbench.action.closeAllEditors');
  result.phase = 'tool-result-last-close-reopen';
  await open(toolUri);
  await expect(text(0).locator('.view-lines')).toContainText('large-output-line-0000');
  expect(result.sources.filter(read => read.phase === result.phase)).toHaveLength(1);
  result.toolArtifactReopen = true;
  result.phase = 'unsupported-source';
  const collection = `boxteam://workspace/${owner.workspaceId}/resources/session`;
  // 原命令直接调用同一 Opener；成功路径已通过 Quick Input，能力拒绝核验精确领域错误。
  const [denied] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/resources/navigation'
      && response.request().postDataJSON()?.uri === collection),
    expect(command('_workbench.open', collection, undefined, undefined, { sideBySide: true }))
      .rejects.toThrow('该资源由领域 owner 访问，不公开文件树'),
  ]);
  const deniedBody = await denied.json();
  expect(denied.status()).toBe(403);
  expect(deniedBody.request_id).toBe(denied.headers()['x-request-id']);
  expect(deniedBody.detail).toMatchObject({ owner: 'resource_uri_resolver', code: 'capability_denied' });
  await expect(groups).toHaveCount(1);
  await expect(text(0).locator('.view-lines')).toContainText('large-output-line-0000');
  expect(result.sources.filter(read => read.phase === result.phase)).toHaveLength(1);
  result.unsupportedSourceRejected = true;
  result.phase = 'copy-file-candidate';
  await command('workbench.action.quickOpen', 'AGENTS.md');
  const candidate = quick.locator('[data-quick-input-id^="boxteam-file:"]').filter({ hasText: 'AGENTS.md' });
  await expect(candidate).toHaveCount(1);
  const [copied] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/resource-addresses/parse'),
    candidate.getByRole('button', { name: '复制资源 URI', exact: true }).click(),
  ]);
  const copiedBody = await copied.json();
  expect(copied.status()).toBe(200);
  expect(copiedBody.request_id).toBe(copied.headers()['x-request-id']);
  expect(copiedBody.data).toMatchObject({ gateway_id: owner.gatewayId, scope_id: owner.workspaceId, kind: 'files' });
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(copiedBody.data.reference_uri);
  await expect(quick.locator('input')).toBeFocused();
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
  expect(result.fileReads.filter(read => read.phase === result.phase)).toEqual([]);
  await expect(groups).toHaveCount(1);
  await expect(text(0).locator('.view-lines')).toContainText('large-output-line-0000');
  await page.keyboard.press('Escape');
  result.fileReferenceCopiedWithoutOpen = true;
  expect(result.fileReads).toEqual([]);
  expect(result.executions).toEqual([{ phase: 'execute-output',
    path: `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`,
    gatewayId: owner.gatewayId, workspaceId: owner.workspaceId }]);
  expect(result.sources.every(read => read.gatewayId === owner.gatewayId && read.workspaceId === owner.workspaceId)).toBe(true);
  expect(result.addressRequests.every(request => request.body.local_gateway_id === owner.gatewayId)).toBe(true);
  result.noExecutionOrFileFallback = true;
  expect(result.pageErrors).toEqual([]);
  result.phase = 'passed';
} catch (error) {
  failure = error; result.error = String(error);
  if (page) {
    try { await page.screenshot({ path: screenshot, fullPage: true }); }
    catch (diagnostic) { failure = new AggregateError([error, diagnostic], '验收和截图均失败'); }
  }
} finally {
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '验收和结果保存均失败') : error; }
  try { await run.close(); }
  catch (error) { failure = failure ? new AggregateError([failure, error], '验收和清理均失败') : error; }
}
if (failure) throw failure;
