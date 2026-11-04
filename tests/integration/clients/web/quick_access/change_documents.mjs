import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';
import { selectThread } from '../persistent_tasks/workbench.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!base || !output || !screenshot) throw new Error('只读模型验收缺少隔离目标或产物目录');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_change_documents.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], reads: [], sources: [],
  boundary: '真实Gateway／后端／正式Workbench；模型协议采用model_stream；产品执行创建文件和更改，无预写历史、缓存或HTTP响应替身' };
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
    if (/\/changesets\/.+$/.test(path)) result.reads.push({ phase: result.phase, path, route: request.headers()['x-boxteam-workspace-id'] });
    if (path === '/api/v1/resources/navigation' && request.postDataJSON()?.operation === 'read') {
      result.sources.push({ phase: result.phase, body: request.postDataJSON() });
    }
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
  await api('/api/gateway/users/guest', { tracking: { source: 'change-documents' } });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces?check_health=false');
  const workspace = await api('/api/v1/workspace');
  const session = await api('/api/v1/sessions', { title: '更改模型验收' });
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`);
  const owner = { gatewayId: health.gateway_id, workspaceId: workspace.workspace_id, routeId: targets.active_workspace_id,
    sessionId: session.session_id, threadId: main.thread_id, title: session.title };
  result.owner = owner;
  const interactions = JSON.parse(process.env.BOXTEAM_CHANGE_INTERACTIONS), turns = [];
  for (let index = 0; index < 2; index++) {
    result.phase = 'execute-' + index;
    await testControl({ action: 'plan_model', session_id: owner.sessionId, thread_id: owner.threadId, interactions: interactions.splice(0, 2) });
    const accepted = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`,
      { message: { content: index ? '更新至第二版' : '生成首版' }, run: {} });
    await expect.poll(async () => {
      const job = await api(`/api/v1/jobs/${accepted.job_id}`);
      if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
      return job.status;
    }, { timeout: 30_000 }).toBe('completed');
    turns.push(accepted.turn_id);
  }
  result.turns = turns;
  const changesetId = 'turn:' + turns[1];
  const data = await api(`/api/v1/sessions/${owner.sessionId}/changesets/${encodeURIComponent(changesetId)}`);
  const file = data.files.find(file => file.file_path.endsWith('/diff-history.txt'));
  expect(file.before_preview).toContain('history v1 line 01'); expect(file.after_preview).toContain('history v2 line 01');
  await page.reload({ waitUntil: 'domcontentloaded' });
  const command = (id, ...args) => page.evaluate(({ id, args }) => globalThis.workbench.commands.executeCommand(id, ...args), { id, args });
  const thread = await selectThread(page, owner, 0);
  await thread.getByRole('button', { name: '切换会话资源', exact: true }).click();
  await thread.getByRole('tab', { name: '更改', exact: true }).click();
  await thread.locator(`.session-changeset-row[title="显示任务 ${turns[1]} 中产生的文件变更"]`).click();
  await expect(thread.locator('.session-change-file-main')).toHaveCount(1);
  await expect(thread.locator('.session-change-file-main')).toContainText('修改');
  result.phase = 'open-real-diff';
  await thread.locator('.session-change-file-main').click();
  const groups = page.locator('.editor-group-container');
  const diff = index => groups.nth(index).locator('.monaco-diff-editor').filter({ visible: true });
  const assertDiff = async index => {
    await expect(diff(index).locator('.editor.original .view-lines[role="presentation"]')).toContainText('history v1 line 01');
    await expect(diff(index).locator('.editor.modified .view-lines[role="presentation"]')).toContainText('history v2 line 01');
  };
  await assertDiff(0);
  expect(result.reads.filter(read => read.phase === 'open-real-diff')).toHaveLength(1);
  result.singleDiffRead = true;
  result.phase = 'split-original-diff';
  await command('workbench.action.splitEditorRight');
  await expect(groups).toHaveCount(2);
  await assertDiff(0); await assertDiff(1);
  expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
  result.splitShared = true;
  const modified = diff(1).locator('.editor.modified');
  await modified.getByRole('textbox').focus(); await page.keyboard.insertText('UNEXPECTED MUTATION');
  await assertDiff(1); await expect(modified.locator('.view-lines[role="presentation"]')).not.toContainText('UNEXPECTED MUTATION');
  result.readonly = true;
  result.phase = 'open-original-patch';
  await command('boxteam.changes.openPatch');
  const text = () => groups.nth(1).locator('.editor-instance .monaco-editor').filter({ visible: true });
  await expect(text().locator('.view-lines')).toContainText('+history v2 line 01');
  result.patch = true;
  await command('workbench.action.closeActiveEditor'); await assertDiff(1);
  result.phase = 'migrate-legacy-resource';
  const legacy = await page.evaluate(({ owner, file, changesetId }) => globalThis.workbench.URI.from({
    scheme: 'boxteam-change', authority: owner.workspaceId,
    path: `/${owner.sessionId}/${changesetId}/after/diff-history.txt`,
    query: JSON.stringify({ owner: { ...owner, routeId: 'retired-route', title: '旧标题' }, changesetId, filePath: file.file_path,
      editId: file.latest_edit_id, phase: 'after' }),
  }).toString(), { owner, file, changesetId });
  await command('_workbench.open', legacy, [1, { pinned: true }]);
  await expect(text().locator('.view-lines')).toContainText('history v2 line 01');
  expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
  result.legacyMigrated = true;
  await command('workbench.action.closeActiveEditor'); await assertDiff(1);
  result.phase = 'diff-refresh-restore';
  // 原正常 Reload Window 会等待生命周期提交；浏览器强制导航不保证异步存储完成。
  const reload = () => Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), command('workbench.action.reloadWindow')]);
  await reload();
  await assertDiff(0); await assertDiff(1);
  expect(result.reads.filter(read => read.phase === result.phase)).toHaveLength(1);
  result.diffRestored = true;
  result.phase = 'readonly-source';
  await command('workbench.action.focusSecondEditorGroup');
  const source = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/agent-spec/root/AGENTS.md`;
  await command('workbench.action.quickOpen', source);
  await page.locator('.quick-input-widget').getByText('打开链接', { exact: true }).click();
  await expect(text()).toHaveCount(1);
  await expect(text().locator('.view-lines')).toContainText('只读来源模型验收');
  result.phase = 'source-refresh-restore';
  await reload();
  await expect(text()).toHaveCount(1);
  await assertDiff(0); await expect(text().locator('.view-lines')).toContainText('只读来源模型验收');
  expect(result.sources.filter(read => read.phase === result.phase)).toHaveLength(1);
  result.sourceRestored = true;
  await command('workbench.action.closeAllEditors');
  result.phase = 'last-close-reopen';
  await command('_workbench.open', legacy, [0, { pinned: true }]);
  await expect(groups.nth(0).locator('.editor-instance .view-lines')).toContainText('history v2 line 01');
  expect(result.reads.filter(read => read.phase === result.phase)).toHaveLength(1);
  result.lastCloseRereads = true;
  expect(result.reads.every(read => read.route === owner.routeId || read.phase.startsWith('execute-'))).toBe(true);
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
