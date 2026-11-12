import { writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`最近历史验收缺少 ${name}`); return value; };
const base = required('BOXTEAM_BROWSER_BASE_URL'), output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_recent_history.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], searches: [], executions: [], receipts: [], threadReads: [], recovery: [],
  boundary: '真实 Gateway／Backend／正式 Workbench。由产品创建Session并生成历史与草稿；恢复负例只在页面卸载后破坏一条真实Thread序列化记录，不预写业务状态。来源为普通文件预备。' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('最近历史 Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1700, height: 1000 } });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/search') result.searches.push({ phase: result.phase, body: request.postDataJSON() });
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executions.push(path);
    if (/^\/api\/v1\/sessions\/[^/]+\//.test(path)) result.threadReads.push({ phase: result.phase, path });
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const savedHistory = () => page.evaluate(async () => {
    const read = request => new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    const histories = [];
    for (const info of await indexedDB.databases()) {
      if (!info.name.startsWith('vscode-web-state-db-')) continue;
      const db = await read(indexedDB.open(info.name));
      try {
        const value = await read(db.transaction('ItemTable').objectStore('ItemTable').get('history.entries'));
        if (value !== undefined) histories.push({ database: info.name, entries: JSON.parse(value) });
      } finally { db.close(); }
    }
    return histories;
  });
  const api = async (path, owner, body) => {
    const receipt = await page.evaluate(async ({ path, owner, body }) => {
      const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
        headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json', ...(owner ? {
          'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
        } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
      return { status: response.status, payload: await response.json(), requestId: response.headers.get('X-Request-ID') };
    }, { path, owner, body });
    expect(receipt.status).toBe(200); expect(receipt.payload.request_id).toBe(receipt.requestId);
    result.receipts.push({ phase: result.phase, path, requestId: receipt.requestId });
    return receipt.payload.data;
  };
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const ready = () => expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  await ready();
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces?check_health=false');
  const ownerA = { gatewayId: health.gateway_id, workspaceId: listing.active_workspace.workspace_id };
  expect((await api('/api/v1/workspace', ownerA)).workspace_id).toBe(ownerA.workspaceId);
  const registered = await api('/api/gateway/managed-workspaces', undefined,
    { root_path: required('BOXTEAM_HISTORY_OTHER_WORKSPACE'), name: 'History B' });
  const other = registered.items.find(item => item.name === 'History B');
  const ownerB = { gatewayId: other.gateway_id, workspaceId: other.workspace_id };
  expect(ownerA.workspaceId).not.toBe(ownerB.workspaceId);
  expect((await api(`/api/gateway/workspaces/${ownerA.workspaceId}/activate`, ownerA, {})).active_workspace)
    .toEqual({ gateway_id: ownerA.gatewayId, workspace_id: ownerA.workspaceId });
  const identities = [];
  for (const owner of [ownerA, ownerB]) {
    const session = await api('/api/v1/sessions', owner, { title: '同名最近会话' });
    const thread = await api(`/api/v1/sessions/${session.session_id}/main-thread`, owner);
    identities.push({ owner, sessionId: session.session_id, threadId: thread.thread_id,
      resource: `boxteam-thread://${owner.gatewayId}/${owner.workspaceId}/${session.session_id}/${thread.thread_id}`,
      publicUri: `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/session/${session.session_id}/threads/${thread.thread_id}` });
  }
  result.identities = identities;
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off', 'search.quickOpen.includeHistory': true });
  const quick = page.locator('.quick-input-widget');
  const pick = resource => quick.locator(`[data-quick-input-id="${resource}"]`);
  const selectHistoryOwner = async owner => {
    await command('workbench.action.quickOpen', '');
    await quick.getByRole('button', { name: '选择搜索范围', exact: true }).click();
    await expect(quick).toContainText('选择快速访问搜索范围');
    for (const checkbox of await quick.getByRole('checkbox').all()) await checkbox.uncheck();
    await quick.locator('.monaco-list-row').filter({ hasText: owner.gatewayId }).filter({ hasText: owner.workspaceId })
      .getByRole('checkbox').check();
    await quick.locator('input').press('Enter');
    await expect(quick.locator('input')).toHaveAttribute('placeholder', `查找会话、Thread 和文件 · ${owner.workspaceId}`);
  };
  const open = async uri => {
    await command('workbench.action.quickOpen', uri);
    await quick.locator(`[data-quick-input-id="link:${uri}"]`).click();
    await expect(quick).not.toBeVisible();
  };
  const threadPane = identity => page.locator(`section.boxteam-thread[data-workspace-id="${identity.owner.workspaceId}"]`
    + `[data-session-id="${identity.sessionId}"][data-thread-id="${identity.threadId}"]`);
  const [a, b] = identities;
  const fileUri = `boxteam://${ownerA.gatewayId}/workspace/${ownerA.workspaceId}/resources/files/root/history.txt`;
  const sourceUri = `boxteam://${ownerA.gatewayId}/workspace/${ownerA.workspaceId}/resources/agent-spec/root/AGENTS.md`;
  const text = () => page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last().locator('.view-lines');
  result.phase = 'open-products-to-create-history';
  await open(fileUri); await expect(text()).toContainText('A history file');
  await open(sourceUri); await expect(text()).toContainText('A history source');
  await open(a.publicUri);
  await threadPane(a).getByTestId('composer-input').fill('A 最近历史草稿');
  await open(b.publicUri);
  await threadPane(b).getByTestId('composer-input').fill('B 最近历史草稿');
  await command('workbench.action.quickOpen', '');
  await expect(pick(b.resource)).toHaveCount(1); await expect(pick(a.resource)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await command('workbench.action.closeAllEditors');
  await expect(page.locator('section.boxteam-thread')).toHaveCount(0);
  await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true })).toHaveCount(0);
  result.phase = 'closed-history-before-reload';
  await command('workbench.action.quickOpen', '');
  await expect(pick(a.resource)).toHaveCount(1); await expect(pick(b.resource)).toHaveCount(0);
  await expect(quick.locator('[data-quick-input-id^="boxteam-file:"]')).toHaveCount(1);
  await expect(quick.locator('[data-quick-input-id^="boxteam-source:"]')).toHaveCount(1);
  result.beforeReload = await quick.locator('[data-quick-input-id]').evaluateAll(rows => rows.map(row => row.dataset.quickInputId));
  await page.keyboard.press('Escape');
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  result.savedBeforeReload = await savedHistory();
  expect(result.savedBeforeReload.flatMap(item => item.entries.map(entry => entry.editor.resource)))
    .toEqual(expect.arrayContaining([a.resource, b.resource, ...result.beforeReload]));
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  result.phase = 'restored-closed-history';
  await expect(page.locator('section.boxteam-thread')).toHaveCount(0);
  await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true })).toHaveCount(0);
  // 冷启动没有发起编辑器；经原范围选择明确A，不以打开A来替产品生成待验历史。
  await selectHistoryOwner(ownerA);
  await expect(pick(a.resource)).toHaveCount(1); await expect(pick(b.resource)).toHaveCount(0);
  const restored = await quick.locator('[data-quick-input-id]').evaluateAll(rows => rows.map(row => row.dataset.quickInputId));
  expect(restored).toEqual(result.beforeReload);
  result.restoredClosedHistory = true;
  await pick(a.resource).click();
  await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
  await command('workbench.action.quickOpen', '');
  await expect(pick(a.resource)).toHaveCount(1); await expect(pick(b.resource)).toHaveCount(0);
  result.reopenDeduplicated = true;
  await pick(a.resource).getByRole('button', { name: '从最近打开中移除', exact: true }).click();
  await expect(pick(a.resource)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
  expect(await api(`/api/v1/sessions/${a.sessionId}/main-thread`, ownerA)).toMatchObject({ thread_id: a.threadId });
  result.removedHistoryOnly = true;
  await command('workbench.action.closeAllEditors');
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  await selectHistoryOwner(ownerA);
  await expect(pick(a.resource)).toHaveCount(0);
  const source = quick.locator('[data-quick-input-id^="boxteam-source:"]');
  await source.click(); await expect(text()).toContainText('A history source');
  await command('workbench.action.quickOpen', '');
  await quick.locator('[data-quick-input-id^="boxteam-file:"]').click(); await expect(text()).toContainText('A history file');
  result.phase = 'other-owner-and-draft-still-restorable';
  await open(b.publicUri);
  await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
  await command('workbench.action.quickOpen', '');
  await expect(pick(b.resource)).toHaveCount(1); await expect(pick(a.resource)).toHaveCount(0);
  expect(await api(`/api/v1/sessions/${b.sessionId}/main-thread`, ownerB)).toMatchObject({ thread_id: b.threadId });
  result.scopedOwners = true; result.draftRestored = true; result.businessPreserved = true;
  await page.keyboard.press('Escape');
  await command('workbench.action.closeAllEditors');
  await command('workbench.action.joinAllGroups');
  await open(a.publicUri);
  const groups = page.locator('.editor-group-container');
  const reload = async () => {
    await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
    await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  };
  for (const mode of ['unknown-version', 'missing-owner', 'broken-json']) {
    result.phase = 'prepare-' + mode;
    await command('workbench.action.focusFirstEditorGroup');
    await command('workbench.action.quickOpen', b.publicUri);
    await quick.locator(`[data-quick-input-id="link:${b.publicUri}"]`).click({ modifiers: ['Alt'] });
    await expect(groups).toHaveCount(2);
    await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
    await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
    // 离开工作台后只破坏原存储的一条真实记录，避免运行中的存储服务覆盖故障样本。
    const storageUrl = new URL('/__thread_recovery_storage__', base).href;
    await page.route(storageUrl, route => route.fulfill({ contentType: 'text/html', body: '<title>Thread恢复存储故障注入</title>' }));
    await page.goto(storageUrl, { waitUntil: 'domcontentloaded' });
    const corruption = await page.evaluate(async ({ mode, sessionId }) => {
      const read = request => new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      const matches = [];
      for (const info of await indexedDB.databases()) {
        if (!info.name.startsWith('vscode-web-state-db-')) continue;
        const db = await read(indexedDB.open(info.name));
        try {
          const table = db.transaction('ItemTable').objectStore('ItemTable');
          const [keys, values] = await Promise.all([read(table.getAllKeys()), read(table.getAll())]);
          for (let index = 0; index < values.length; index++) {
            let state;
            try { state = JSON.parse(values[index]); } catch { continue; }
            if (!state || typeof state !== 'object' || !state['editorpart.state']) continue;
            const changed = [];
            const visit = value => {
              if (!value || typeof value !== 'object') return;
              if (Array.isArray(value.editors)) for (const editor of value.editors) {
                if (editor.id !== 'boxteam.editor.thread') continue;
                const stored = JSON.parse(editor.value);
                if (stored.target?.sessionId !== sessionId) continue;
                const replacement = mode === 'broken-json' ? '{"version":2,' : JSON.stringify(mode === 'unknown-version'
                  ? { ...stored, version: 42 } : { ...stored, target: { ...stored.target, gatewayId: '' } });
                changed.push({ original: editor.value, replacement }); editor.value = replacement;
              }
              for (const child of Object.values(value)) visit(child);
            };
            visit(state['editorpart.state']);
            if (changed.length) matches.push({ database: info.name, key: keys[index], value: JSON.stringify(state), changed });
          }
        } finally { db.close(); }
      }
      if (matches.length !== 1 || matches[0].changed.length !== 1) throw new Error(`预期一条Thread记录，实际${JSON.stringify(matches)}`);
      const match = matches[0], db = await read(indexedDB.open(match.database));
      try {
        await new Promise((resolve, reject) => {
          const transaction = db.transaction('ItemTable', 'readwrite');
          transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error);
          transaction.onabort = () => reject(transaction.error ?? new Error('Thread恢复故障写入中止'));
          transaction.objectStore('ItemTable').put(match.value, match.key);
        });
      } finally { db.close(); }
      return { mode, database: match.database, key: match.key, ...match.changed[0] };
    }, { mode, sessionId: b.sessionId });
    await page.unroute(storageUrl);
    const reason = mode === 'unknown-version' ? '不支持的 Thread 编辑器恢复版本：42'
      : mode === 'missing-owner' ? 'Thread 编辑器恢复数据缺少精确 owner' : 'Thread 编辑器恢复 JSON 损坏';
    result.phase = 'recover-' + mode;
    await page.goto(base, { waitUntil: 'domcontentloaded' }); await ready();
    const placeholder = page.locator('.monaco-editor-pane-placeholder').filter({ visible: true });
    await expect(placeholder).toContainText(reason);
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
    await expect(threadPane(b)).toHaveCount(0);
    result.phase = 'retain-' + mode;
    await reload();
    await expect(placeholder).toContainText(reason);
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
    await placeholder.getByRole('button', { name: '移除此展示记录', exact: true }).click();
    await expect(placeholder).toHaveCount(0);
    result.phase = 'remove-' + mode;
    await reload();
    await expect(page.locator('.editor-placeholder-label-container').filter({ hasText: reason })).toHaveCount(0);
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
    await expect(threadPane(b)).toHaveCount(0);
    expect(result.threadReads.filter(read => /^(recover|retain|remove)-/.test(read.phase)
      && read.path.startsWith(`/api/v1/sessions/${b.sessionId}/`))).toEqual([]);
    result.phase = 'verify-business-' + mode;
    expect(await api(`/api/v1/sessions/${b.sessionId}/main-thread`, ownerB)).toMatchObject({ thread_id: b.threadId });
    result.recovery.push({ ...corruption, retainedAcrossReload: true, displayOnlyRemoval: true, targetReads: 0 });
    // 单条移除不删除业务、原草稿或最近历史；下一轮仍从同一规范引用打开。
  }
  result.phase = 'reopen-after-display-removal';
  await open(b.publicUri);
  await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
  expect(result.searches).toEqual([]); expect(result.executions).toEqual([]); expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error instanceof Error ? error.stack : String(error);
  if (page) await page.screenshot({ path: join(dirname(output), 'failure.png') }).catch(cause => { result.screenshotError = String(cause); });
} finally {
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
  await run.close();
}
if (failure) throw failure;
