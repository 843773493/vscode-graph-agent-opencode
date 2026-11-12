import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';
import { verifyEditorNavigation, verifyViewNavigation } from './editor_navigation.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL, output = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshot = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!base || !output || !screenshot) throw new Error('只读模型验收缺少隔离目标或产物目录');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_change_documents.py');
const result = { classification: 'Integration', phase: 'prepare', pageErrors: [], consoleErrors: [], reads: [], sources: [], mcpReads: [], changesetLists: [], reviews: [], executions: [], pastes: [],
  boundary: '真实Gateway／后端／正式Workbench；模型协议采用model_stream；产品创建更改及编辑器状态，仅恢复故障阶段改坏一条真实只读编辑器记录' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1050 } });
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push({ phase: result.phase, text: message.text() }); });
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/messages') && request.method() === 'POST') result.executions.push({ phase: result.phase, path });
    if (path.endsWith('/changesets')) result.changesetLists.push({ phase: result.phase, path,
      gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    if (path.endsWith('/review')) result.reviews.push({ phase: result.phase, method: request.method(), path });
    if (path === '/api/v1/mcp/servers') result.mcpReads.push({ path,
      gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    if (/\/changesets\/.+$/.test(path)) result.reads.push({ phase: result.phase, path,
      gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    if (path === '/api/v1/resources/navigation' && request.postDataJSON()?.operation === 'read') {
      result.sources.push({ phase: result.phase, body: request.postDataJSON(),
        gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] });
    }
  });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const api = (path, data, owner) => page.evaluate(async ({ path, data, owner }) => {
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
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const health = await api('/api/gateway/health'), targets = await api('/api/gateway/workspaces?check_health=false');
  const target = targets.active_workspace;
  expect(target.gateway_id).toBe(health.gateway_id);
  const workspaceOwner = { gatewayId: target.gateway_id, workspaceId: target.workspace_id };
  const workspace = await api('/api/v1/workspace', undefined, workspaceOwner);
  expect(workspace.workspace_id).toBe(target.workspace_id);
  const session = await api('/api/v1/sessions', { title: '更改模型验收' }, workspaceOwner);
  expect(session.workspace_id).toBe(target.workspace_id);
  const main = await api(`/api/v1/sessions/${session.session_id}/main-thread`, undefined, workspaceOwner);
  expect(main.session_id).toBe(session.session_id);
  const owner = { ...workspaceOwner,
    sessionId: session.session_id, threadId: main.thread_id, title: session.title };
  result.owner = owner;
  const interactions = JSON.parse(process.env.BOXTEAM_CHANGE_INTERACTIONS), turns = [];
  for (let index = 0; index < 2; index++) {
    result.phase = 'execute-' + index;
    await testControl({ action: 'plan_model', session_id: owner.sessionId, thread_id: owner.threadId, interactions: interactions.splice(0, 2) });
    const accepted = await api(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`,
      { message: { content: index ? '更新至第二版' : '生成首版' }, run: {} }, owner);
    await expect.poll(async () => {
      const job = await api(`/api/v1/jobs/${accepted.job_id}`, undefined, owner);
      if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
      return job.status;
    }, { timeout: 30_000 }).toBe('completed');
    turns.push(accepted.turn_id);
  }
  result.turns = turns;
  const changesetId = 'turn:' + turns[1];
  const data = await api(`/api/v1/sessions/${owner.sessionId}/changesets/${encodeURIComponent(changesetId)}`, undefined, owner);
  const file = data.files.find(file => file.file_path.endsWith('/diff-history.txt'));
  expect(file.before_preview).toContain('history v1 line 01'); expect(file.after_preview).toContain('history v2 line 01');
  await page.reload({ waitUntil: 'domcontentloaded' });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const workspaceKey = JSON.stringify([owner.gatewayId, owner.workspaceId]);
  await page.getByTestId(`catalog-node-${workspaceKey}-${owner.sessionId}`).locator(`button[data-session-id="${owner.sessionId}"]`).dblclick();
  const thread = page.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(thread).toBeVisible();
  const quick = page.locator('.quick-input-widget');
  const chosen = quick.locator('.monaco-list-row').filter({ hasText: '会话变更 ·' }).filter({ hasText: changesetId });
  result.phase = 'browse-changesets';
  await command('boxteam.quickAccess.resources');
  await quick.locator('input').first().fill('resource ' + changesetId);
  await expect(chosen).toBeVisible();
  await quick.locator('input').first().fill('resource all');
  await expect(quick.locator('.monaco-list-row').filter({ hasText: '会话变更 ·' }).filter({ hasText: ' · all' })).toBeVisible();
  await page.keyboard.press('Escape');
  const listings = result.changesetLists.filter(read => read.phase === result.phase);
  expect(listings).toEqual([{ phase: result.phase, path: `/api/v1/sessions/${owner.sessionId}/changesets`,
    gatewayId: owner.gatewayId, workspaceId: owner.workspaceId }]);
  expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
  expect(result.reviews).toEqual([]);
  result.changesetBrowseOnly = true;
  result.phase = 'accept-changeset';
  await command('boxteam.quickAccess.resources');
  await quick.locator('input').first().fill('resource ' + changesetId);
  await expect(chosen).toBeVisible(); await chosen.click();
  await expect(thread.getByRole('tab', { name: '更改', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(thread.locator(`.session-changeset-row[title="显示任务 ${turns[1]} 中产生的文件变更"]`)).toHaveClass(/active/);
  result.changesetOpened = true;
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
  // 原 Thread 保持可见；Diff 左右及 patch 的选择均交同一个草稿 writer。
  await groups.nth(0).getByRole('tab').filter({ hasText: owner.title }).click();
  const composer = thread.locator('textarea[data-testid="composer-input"]');
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue('');
  await expect(thread.locator('.session-change-file-main')).toBeVisible();
  let pasted = '';
  const paste = async (editor, expected, wholeDocument = false) => {
    await editor.getByRole('textbox').focus();
    await page.keyboard.press('ControlOrMeta+Home');
    await page.keyboard.press(wholeDocument ? 'ControlOrMeta+A' : 'Shift+End');
    const pending = command('boxteam.resource.pasteSelectionToChat');
    await expect(quick).toContainText('选择接收草稿的 Thread');
    await quick.getByRole('option').filter({ hasText: owner.threadId }).click();
    await pending;
    pasted += (pasted ? '\n' : '') + expected;
    await expect(composer).toHaveValue(pasted);
    expect(result.reads.filter(read => read.phase === result.phase)).toEqual([]);
    expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
    expect(result.executions).toHaveLength(2);
    result.pastes.push({ phase: result.phase, content: expected });
  };
  result.phase = 'paste-diff-original';
  await paste(diff(1).locator('.editor.original'), 'history v1 line 01');
  result.phase = 'paste-diff-modified';
  await paste(modified, 'history v2 line 01');
  result.phase = 'open-original-patch';
  await command('boxteam.changes.openPatch');
  const text = () => groups.nth(1).locator('.editor-instance .monaco-editor').filter({ visible: true });
  await expect(text().locator('.view-lines')).toContainText('+history v2 line 01');
  result.patch = true;
  result.phase = 'paste-patch';
  await paste(text(), file.diff_text, true);
  await command('workbench.action.closeActiveEditor'); await assertDiff(1);
  await groups.nth(0).getByRole('tab').filter({ hasText: 'diff-history.txt' }).click();
  await assertDiff(0);
  await command('workbench.action.focusSecondEditorGroup');
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
  await groups.nth(0).getByRole('tab').filter({ hasText: owner.title }).click();
  await expect(composer).toHaveValue(pasted);
  result.pasteRestored = true;
  await groups.nth(0).getByRole('tab').filter({ hasText: 'diff-history.txt' }).click();
  await command('workbench.action.focusSecondEditorGroup');
  result.phase = 'readonly-source';
  await command('workbench.action.focusSecondEditorGroup');
  const source = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/agent-spec/root/AGENTS.md`;
  await command('workbench.action.quickOpen', source);
  await page.locator('.quick-input-widget').getByText('打开链接', { exact: true }).click();
  await expect(text()).toHaveCount(1);
  await expect(text().locator('.view-lines')).toContainText('只读来源模型验收');
  result.phase = 'split-readonly-source';
  await command('workbench.action.splitEditorRight');
  await expect(groups).toHaveCount(3);
  const sourceText = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true });
  await expect(sourceText(2).locator('.view-lines')).toContainText('只读来源模型验收');
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
  await sourceText(2).getByRole('textbox').focus();
  await page.keyboard.insertText('UNEXPECTED SOURCE MUTATION');
  await expect(sourceText(2).locator('.view-lines')).not.toContainText('UNEXPECTED SOURCE MUTATION');
  await command('workbench.action.closeActiveEditor');
  await expect(groups).toHaveCount(2);
  await expect(text().locator('.view-lines')).toContainText('只读来源模型验收');
  expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
  result.sourceSplitShared = true;
  result.phase = 'source-refresh-restore';
  await reload();
  await expect(text()).toHaveCount(1);
  await assertDiff(0); await expect(text().locator('.view-lines')).toContainText('只读来源模型验收');
  expect(result.sources.filter(read => read.phase === result.phase)).toHaveLength(1);
  result.sourceRestored = true;
  result.readonlyRecovery = [];
  for (const mode of ['unknown-version', 'broken-json']) {
    result.phase = 'persist-source-before-' + mode;
    await reload();
    await expect(text().locator('.view-lines')).toContainText('只读来源模型验收');
    // 产品已提交正常组状态；离开产品文档后，仅在具名存储故障边界破坏这条来源记录。
    const storageUrl = new URL('/__readonly_recovery_storage__', base).href;
    await page.route(storageUrl, route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>恢复故障注入</title>' }));
    await page.goto(storageUrl);
    const corruption = await page.evaluate(async mode => {
      const read = request => new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      const matches = [];
      for (const info of await indexedDB.databases()) {
        if (!info.name.startsWith('vscode-web-state-db-')) continue;
        const db = await read(indexedDB.open(info.name));
        try {
          const store = db.transaction('ItemTable').objectStore('ItemTable');
          const [keys, values] = await Promise.all([read(store.getAllKeys()), read(store.getAll())]);
          for (let index = 0; index < keys.length; index++) {
            if (typeof values[index] !== 'string' || !values[index].includes('"editorpart.state"')) continue;
            const state = JSON.parse(values[index]);
            const changed = [];
            const visit = value => {
              if (!value || typeof value !== 'object') return;
              if (Array.isArray(value.editors)) {
                for (const editor of value.editors) {
                  if (editor.id !== 'workbench.editors.resourceEditorInput') continue;
                  const stored = JSON.parse(editor.value);
                  if (stored.resource?.scheme !== 'boxteam-source') continue;
                  const replacement = mode === 'unknown-version' ? JSON.stringify({ ...stored, version: 42 }) : '{"version":1,';
                  changed.push({ original: editor.value, replacement }); editor.value = replacement;
                }
              }
              for (const child of Object.values(value)) visit(child);
            };
            visit(state['editorpart.state']);
            if (changed.length) matches.push({ database: info.name, key: keys[index], changed, value: JSON.stringify(state) });
          }
        } finally { db.close(); }
      }
      if (matches.length !== 1 || matches[0].changed.length !== 1) throw new Error(`预期唯一正常来源记录，实际${JSON.stringify(matches)}`);
      const match = matches[0], db = await read(indexedDB.open(match.database));
      try {
        await new Promise((resolve, reject) => {
          const transaction = db.transaction('ItemTable', 'readwrite');
          transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error);
          transaction.onabort = () => reject(transaction.error ?? new Error('恢复故障写入中止'));
          transaction.objectStore('ItemTable').put(match.value, match.key);
        });
      } finally { db.close(); }
      return { mode, database: match.database, key: match.key, ...match.changed[0] };
    }, mode);
    await page.unroute(storageUrl);
    result.phase = 'recover-' + mode;
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    const placeholder = groups.nth(1).locator('.monaco-editor-pane-placeholder').filter({ visible: true });
    const reason = mode === 'unknown-version' ? '不支持的只读文档恢复版本：42' : '只读文档恢复 JSON 损坏';
    await expect(placeholder).toContainText(reason);
    await assertDiff(0);
    expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
    result.phase = 'retain-' + mode;
    await reload();
    await expect(placeholder).toContainText(reason);
    await assertDiff(0);
    expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
    await placeholder.getByRole('button', { name: '移除此展示记录', exact: true }).click();
    await expect(placeholder).toHaveCount(0);
    result.phase = 'removed-' + mode;
    await reload();
    await expect(page.locator('.editor-placeholder-label-container').filter({ hasText: reason })).toHaveCount(0);
    await assertDiff(0);
    expect(result.sources.filter(read => read.phase === result.phase)).toEqual([]);
    result.readonlyRecovery.push({ ...corruption, retainedAcrossReload: true, removedOnlyDisplayRecord: true, sourceReads: 0 });
    await command('workbench.action.focusSecondEditorGroup');
    await command('workbench.action.quickOpen', source);
    await page.locator('.quick-input-widget').getByText('打开链接', { exact: true }).click();
    await expect(text()).toHaveCount(1);
    await expect(text().locator('.view-lines')).toContainText('只读来源模型验收');
  }
  await verifyEditorNavigation({ page, command, owner, fileUri: file.file_path, result });
  await verifyViewNavigation({ page, command, owner, result });
  await command('workbench.action.closeAllEditors');
  result.phase = 'last-close-reopen';
  await command('_workbench.open', legacy, [0, { pinned: true }]);
  await expect(groups.nth(0).locator('.editor-instance .view-lines')).toContainText('history v2 line 01');
  expect(result.reads.filter(read => read.phase === result.phase)).toHaveLength(1);
  result.lastCloseRereads = true;
  expect([...result.reads, ...result.sources].every(read => read.gatewayId === owner.gatewayId && read.workspaceId === owner.workspaceId)).toBe(true);
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
