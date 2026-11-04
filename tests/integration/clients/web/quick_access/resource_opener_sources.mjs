import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!base || !output || !screenshot) throw new Error('规范来源验收缺少隔离目标或产物目录');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_resource_opener_sources.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], sources: [], links: [], addressRequests: [], fileReads: [], executions: [],
  boundary: '真实Gateway／后端／正式Workbench；model_stream精确回放；产品执行产生工具结果及规范引用，不预写历史、浏览器存储或HTTP响应' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1050 } });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/resource-addresses/parse') result.addressRequests.push({ phase: result.phase, body: request.postDataJSON() });
    if (path === '/api/v1/resources/navigation' && request.postDataJSON()?.operation === 'read') {
      result.sources.push({ phase: result.phase, body: request.postDataJSON(), route: request.headers()['x-boxteam-workspace-id'] });
    }
    if (/\/workspace\/files\/(raw|content)$/.test(path)) result.fileReads.push({ phase: result.phase, path });
    if (path.endsWith('/messages') && request.method() === 'POST') result.executions.push({ phase: result.phase, path });
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const api = (path, data) => page.evaluate(async ({ path, data }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST',
      headers: { 'X-Local-Token': token, 'Content-Type': 'application/json' }, body: data === undefined ? undefined : JSON.stringify(data) });
    const payload = await response.json();
    if (!response.ok || payload.code !== 0 || payload.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, payload }));
    }
    return payload.data;
  }, { path, data });
  await api('/api/gateway/users/guest', { tracking: { source: 'resource-opener-sources' } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces?check_health=false');
  const workspace = await api('/api/v1/workspace');
  const session = await api('/api/v1/sessions', { title: '规范工具结果来源验收' });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`);
  const owner = { gatewayId: health.gateway_id, workspaceId: workspace.workspace_id, routeId: targets.active_workspace_id,
    sessionId: session.session_id, threadId: main.thread_id };
  result.owner = owner;
  result.phase = 'execute-output';
  await testControl({ action: 'plan_model', session_id: owner.sessionId, thread_id: owner.threadId,
    interactions: JSON.parse(process.env.BOXTEAM_SOURCE_INTERACTIONS) });
  const accepted = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`,
    { message: { content: '生成完整工具结果' }, run: {} });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 30_000 }).toBe('completed');
  const history = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`,
    { turn_ids: [accepted.turn_id], include: ['tool_result'] });
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
  result.phase = 'canonical-tool-result';
  const address = await open(toolUri, ['Alt']);
  expect(address).toMatchObject({ kind: 'session', session_id: null });
  await expect(groups).toHaveCount(2);
  await expect(text(0).locator('.view-lines')).toContainText('资源打开归属基线');
  await expect(text(1).locator('.view-lines')).toContainText('large-output-line-0000');
  await expect(page.locator('section.boxteam-thread')).toHaveCount(0);
  await expect(groups.nth(1).locator('.tab.active')).toHaveAttribute('data-resource-name', /^tool_[0-9a-f]{24}\.txt$/);
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([
    { phase: result.phase, route: owner.routeId, body: { uri: address.uri, operation: 'read', read_format: 'auto',
      max_bytes: 33554432, max_response_bytes: 50331648 } },
  ]);
  result.toolArtifactOpened = true;
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
  expect(result.fileReads).toEqual([]);
  expect(result.executions).toEqual([{ phase: 'execute-output',
    path: `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages` }]);
  expect(result.sources.every(read => read.route === owner.routeId)).toBe(true);
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
