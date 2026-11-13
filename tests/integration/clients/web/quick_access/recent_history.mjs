import { rename, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`最近历史验收缺少 ${name}`); return value; };
export async function runRecentHistory({ run, launch }) {
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], searches: [], executions: [], receipts: [], threadReads: [], recovery: [],
  boundary: '真实 Gateway／Backend／正式 Workbench。由产品创建Session并生成历史与草稿；恢复负例只在页面卸载后破坏一条真实Thread序列化记录，不预写业务状态。来源为普通文件预备。' };
let page, failure;
try {
  const host = await launch({ run, result, usePage(next) {
  page = next;
  page.setDefaultTimeout(15_000);
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/gateway/search') result.searches.push({ phase: result.phase, body: request.postDataJSON() });
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(path)) result.executions.push(path);
    if (/^\/api\/v1\/sessions\/[^/]+\//.test(path)) result.threadReads.push({ phase: result.phase, path });
  });
  } });
  const base = host.baseUrl;
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
  const api = async (path, owner, body, method = body === undefined ? 'GET' : 'POST') => {
    const receipt = await page.evaluate(async ({ path, owner, body, method }) => {
      const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { method,
        headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json', ...(owner ? {
          'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
        } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
      return { status: response.status, payload: await response.json(), requestId: response.headers.get('X-Request-ID') };
    }, { path, owner, body, method });
    expect(receipt.status).toBe(200); expect(receipt.payload.request_id).toBe(receipt.requestId);
    result.receipts.push({ phase: result.phase, path, requestId: receipt.requestId });
    return receipt.payload.data;
  };
  const rejectCredential = async operation => {
    const pattern = '**/api/gateway/workspaces?check_health=false';
    let receipt, sent = false;
    const reject = async route => {
      if (sent) { await route.continue(); return; }
      sent = true;
      // 只在真实HTTP发送边界置入失效凭据，401及request_id均由原Gateway生成。
      const response = await route.fetch({ headers: { ...route.request().headers(), 'x-local-token': 'expired-history-test-credential' } });
      const body = await response.json();
      receipt = { status: response.status(), detail: body.detail, requestId: body.request_id,
        header: response.headers()['x-request-id'] };
      expect(receipt.status).toBe(401); expect(receipt.detail).toBe('invalid local token');
      expect(receipt.requestId).toBe(receipt.header); expect(receipt.requestId).toBeTruthy();
      await route.fulfill({ response });
    };
    await page.route(pattern, reject);
    try { await operation(); expect(receipt).toBeTruthy(); return receipt; }
    finally { await page.unroute(pattern, reject); }
  };
  const ready = async () => {
    await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('.native-session-navigation [data-testid^="workspace-node-"]').first())
      .toBeVisible({ timeout: 30_000 });
  };
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
  const settings = { 'files.autoSave': 'off', 'search.quickOpen.includeHistory': true,
    'editor.find.seedSearchStringFromSelection': false };
  await writeWorkspaceSettings({ page, command }, settings);
  const quick = () => page.locator('.quick-input-widget');
  const pick = resource => quick().locator(`[data-quick-input-id="${resource}"]`);
  const selectHistoryOwner = async owner => {
    await command('workbench.action.quickOpen', '');
    await quick().getByRole('button', { name: '选择搜索范围', exact: true }).click();
    await expect(quick()).toContainText('选择快速访问搜索范围');
    for (const checkbox of await quick().getByRole('checkbox').all()) await checkbox.uncheck();
    await quick().locator('.monaco-list-row').filter({ hasText: owner.gatewayId }).filter({ hasText: owner.workspaceId })
      .getByRole('checkbox').check();
    await quick().locator('input').press('Enter');
    await expect(quick().locator('input')).toHaveAttribute('placeholder', `查找会话、Thread 和文件 · ${owner.workspaceId}`);
  };
  const open = async uri => {
    await command('workbench.action.quickOpen', '');
    await expect(quick()).toBeVisible();
    await quick().locator('input').fill(uri);
    await quick().locator(`[data-quick-input-id="link:${uri}"]`).click();
    await expect(quick()).not.toBeVisible();
  };
  const threadPane = identity => page.locator(`section.boxteam-thread[data-workspace-id="${identity.owner.workspaceId}"]`
    + `[data-session-id="${identity.sessionId}"][data-thread-id="${identity.threadId}"]`);
  const [a, b] = identities;
  result.phase = 'original-input-memory-and-command-history';
  const commandId = 'workbench.action.closeAllEditors';
  const commandPick = () => quick().locator(`[data-quick-input-id="${commandId}"]`);
  const exerciseInputMemory = async preserve => {
    await writeWorkspaceSettings({ page, command }, { ...settings,
      'workbench.quickOpen.preserveInput': preserve, 'workbench.commandPalette.preserveInput': preserve,
      'search.quickAccess.preserveInput': preserve });
    await command('workbench.action.quickOpen', a.publicUri);
    await quick().locator(`[data-quick-input-id="link:${a.publicUri}"]`).click();
    await expect(threadPane(a).getByTestId('composer-input')).toBeEditable();
    await command('workbench.action.quickOpen');
    await expect(quick().locator('input')).toHaveValue(preserve ? a.publicUri : '');
    const defaultValue = await quick().locator('input').inputValue();
    await page.keyboard.press('Escape');
    await expect(quick()).not.toBeVisible();
    await command('workbench.action.quickOpen', '%A history file');
    await quick().locator('.monaco-list-row').filter({ hasText: 'A history file' }).last().click();
    await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last().locator('.view-lines'))
      .toContainText('A history file');
    await command('workbench.action.quickTextSearch');
    await expect(quick().locator('input')).toHaveValue(preserve ? '%A history file' : '%');
    const textValue = await quick().locator('input').inputValue();
    await page.keyboard.press('Escape');
    await expect(quick()).not.toBeVisible();
    await command('workbench.action.showCommands');
    await expect(quick()).toBeVisible();
    await expect(quick().locator('input')).toBeFocused();
    await quick().locator('input').fill('>关闭所有编辑器');
    await expect(commandPick()).toBeVisible();
    await commandPick().click();
    await expect(quick()).not.toBeVisible();
    await command('workbench.action.showCommands');
    await expect(quick().locator('input')).toHaveValue(preserve ? '>关闭所有编辑器' : '>');
    const commandValue = await quick().locator('input').inputValue();
    await quick().locator('input').fill('>');
    await expect(commandPick()).toBeVisible();
    await expect(commandPick().getByRole('button', { name: '从最近使用的内容中移除', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(quick()).not.toBeVisible();
    return { preserve, defaultRestored: defaultValue === a.publicUri, commandRestored: commandValue === '>关闭所有编辑器',
      textRestored: textValue === '%A history file', values: { defaultValue, commandValue, textValue } };
  };
  result.inputMemory = [await exerciseInputMemory(true), await exerciseInputMemory(false)];
  await host.reload(); await ready();
  await command('workbench.action.showCommands');
  await expect(commandPick()).toBeVisible();
  const removeCommand = commandPick().getByRole('button', { name: '从最近使用的内容中移除', exact: true });
  await removeCommand.click();
  await page.keyboard.press('Escape');
  await command('workbench.action.showCommands');
  await quick().locator('input').fill('>关闭所有编辑器');
  await expect(commandPick()).toBeVisible();
  await expect(commandPick().getByRole('button', { name: '从最近使用的内容中移除', exact: true })).toHaveCount(0);
  result.commandHistory = { originalCommandId: commandId, restoredAfterRestart: true, displayOnlyRemoval: true };
  await page.keyboard.press('Escape');
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
  await expect(quick().locator('[data-quick-input-id^="boxteam-file:"]')).toHaveCount(1);
  await expect(quick().locator('[data-quick-input-id^="boxteam-source:"]')).toHaveCount(1);
  result.beforeReload = await quick().locator('[data-quick-input-id]').evaluateAll(rows => rows.map(row => row.dataset.quickInputId));
  await page.keyboard.press('Escape');
  await host.reload(); await ready();
  result.savedAfterRestart = await savedHistory();
  expect(result.savedAfterRestart.flatMap(item => item.entries.map(entry => entry.editor.resource)))
    .toEqual(expect.arrayContaining([a.resource, b.resource, ...result.beforeReload]));
  result.phase = 'restored-closed-history';
  await expect(page.locator('section.boxteam-thread')).toHaveCount(0);
  await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true })).toHaveCount(0);
  // 冷启动没有发起编辑器；经原范围选择明确A，不以打开A来替产品生成待验历史。
  await selectHistoryOwner(ownerA);
  await expect(pick(a.resource)).toHaveCount(1); await expect(pick(b.resource)).toHaveCount(0);
  const restored = await quick().locator('[data-quick-input-id]').evaluateAll(rows => rows.map(row => row.dataset.quickInputId));
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
  await host.reload(); await ready();
  await selectHistoryOwner(ownerA);
  await expect(pick(a.resource)).toHaveCount(0);
  const source = quick().locator('[data-quick-input-id^="boxteam-source:"]');
  await source.click(); await expect(text()).toContainText('A history source');
  await command('workbench.action.quickOpen', '');
  await quick().locator('[data-quick-input-id^="boxteam-file:"]').click(); await expect(text()).toContainText('A history file');
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
  const groups = () => page.locator('.editor-group-container');
  const reload = async () => { await host.reload(); await ready(); };
  for (const mode of ['unknown-version', 'missing-owner', 'broken-json']) {
    result.phase = 'prepare-' + mode;
    await command('workbench.action.focusFirstEditorGroup');
    await command('workbench.action.quickOpen', b.publicUri);
    await quick().locator(`[data-quick-input-id="link:${b.publicUri}"]`).click({ modifiers: ['Alt'] });
    await expect(groups()).toHaveCount(2);
    await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
    await host.beforeStorageFault(); await ready();
    await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
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
    await host.restoreAfterFault(); await ready();
    const placeholder = () => page.locator('.monaco-editor-pane-placeholder').filter({ visible: true });
    await expect(placeholder()).toContainText(reason);
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
    await expect(threadPane(b)).toHaveCount(0);
    result.phase = 'retain-' + mode;
    await reload();
    await expect(placeholder()).toContainText(reason);
    await expect(threadPane(a).getByTestId('composer-input')).toHaveValue('A 最近历史草稿');
    await placeholder().getByRole('button', { name: '移除此展示记录', exact: true }).click();
    await expect(placeholder()).toHaveCount(0);
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

  result.phase = 'real-401-invalidates-recent-candidates';
  await command('workbench.action.quickOpen', '');
  await expect(pick(b.resource)).toHaveCount(1);
  let revokedCommand;
  const revoke = async () => {
    revokedCommand = await command('boxteam.workspace.activate', ownerA)
      .then(() => ({ succeeded: true }), cause => ({ error: String(cause) }));
    expect(revokedCommand.error).toContain('Gateway 或访问凭据已改变');
  };
  const candidate401 = await rejectCredential(revoke);
  await expect(quick()).toContainText('Gateway 或访问凭据已改变，请重新打开快速访问');
  await expect(pick(b.resource)).toHaveCount(0);
  await expect(quick().getByRole('button', { name: '从最近打开中移除', exact: true })).toHaveCount(0);
  await page.keyboard.press('Escape');

  result.phase = 'real-401-cancels-history-scope-picker';
  await command('workbench.action.quickOpen', '');
  await quick().getByRole('button', { name: '选择搜索范围', exact: true }).click();
  await expect(quick()).toContainText('选择快速访问搜索范围');
  const scope401 = await rejectCredential(revoke);
  await expect(quick()).not.toBeVisible();

  result.phase = 'real-401-rejects-history-owner-before-surface';
  await command('workbench.action.closeAllEditors');
  await selectHistoryOwner(ownerB);
  await expect(pick(b.resource)).toHaveCount(1);
  const beforeRejectedOwner = result.threadReads.length;
  const owner401 = await rejectCredential(async () => {
    await pick(b.resource).click();
    await expect(page.locator('.monaco-editor-pane-placeholder').filter({ visible: true }))
      .toContainText('Gateway 或访问凭据已改变，请重新打开资源');
  });
  await expect(threadPane(b)).toHaveCount(0);
  expect(result.threadReads.slice(beforeRejectedOwner)).toEqual([]);
  await command('workbench.action.closeAllEditors');
  await selectHistoryOwner(ownerB);
  // 原 HistoryService 会移除打开失败的该条历史；重开仍从原规范引用取得最新路由。
  await expect(pick(b.resource)).toHaveCount(0);
  await open(b.publicUri);
  await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
  result.credentialRecovery = { candidate401, scope401, owner401, rejectedBeforeBusinessReads: true,
    failedHistoryRemoved: true, freshReopen: true };

  result.phase = 'closed-history-keeps-stable-owner-when-path-is-reused';
  await command('workbench.action.closeAllEditors');
  await selectHistoryOwner(ownerB);
  await expect(pick(b.resource)).toHaveCount(1);
  const oldRegistration = (await api('/api/gateway/workspaces?check_health=false')).items
    .find(item => item.gateway_id === ownerB.gatewayId && item.workspace_id === ownerB.workspaceId);
  await api(`/api/gateway/managed-workspaces/${ownerB.workspaceId}?force=true`, undefined, {
    expected_backend_generation: oldRegistration.backend_generation,
    expected_workspace_ids: oldRegistration.affected_workspace_ids,
  }, 'DELETE');
  const oldRoot = required('BOXTEAM_HISTORY_OTHER_WORKSPACE'), relocatedRoot = oldRoot + '-relocated';
  await rename(oldRoot, relocatedRoot);
  await rename(required('BOXTEAM_HISTORY_REPLACEMENT_WORKSPACE'), oldRoot);
  await api('/api/gateway/managed-workspaces', undefined, { root_path: oldRoot, name: 'History replacement C' });
  const replacement = (await api('/api/gateway/workspaces?check_health=false')).items.find(item => item.name === 'History replacement C');
  expect(replacement.workspace_id).not.toBe(ownerB.workspaceId);
  const beforeMissing = result.threadReads.length;
  await pick(b.resource).click();
  await expect(page.locator('.monaco-editor-pane-placeholder').filter({ visible: true })).toContainText('目标 Gateway／UUID 缺少唯一已登记工作区');
  await expect(threadPane(b)).toHaveCount(0);
  expect(result.threadReads.slice(beforeMissing)).toEqual([]);
  result.phase = 'canonical-reference-resolves-relocated-registration';
  await api('/api/gateway/managed-workspaces', undefined, { root_path: relocatedRoot, name: 'History relocated B' });
  const relocated = (await api('/api/gateway/workspaces?check_health=false')).items.find(item => item.name === 'History relocated B');
  expect(relocated.workspace_id).toBe(ownerB.workspaceId); expect(relocated.gateway_id).toBe(ownerB.gatewayId);
  expect(relocated.backend_generation).not.toBe(oldRegistration.backend_generation);
  await command('workbench.action.closeAllEditors');
  await selectHistoryOwner(ownerB);
  await expect(pick(b.resource)).toHaveCount(0);
  await open(b.publicUri);
  await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
  await command('workbench.action.closeAllEditors');
  await selectHistoryOwner(ownerB);
  await expect(pick(b.resource)).toHaveCount(1);
  await pick(b.resource).click();
  await expect(threadPane(b).getByTestId('composer-input')).toHaveValue('B 最近历史草稿');
  expect(await api(`/api/v1/sessions/${b.sessionId}/main-thread`, ownerB)).toMatchObject({ thread_id: b.threadId });
  result.routeRecovery = { previous: { gatewayId: ownerB.gatewayId, workspaceId: ownerB.workspaceId,
    root: oldRoot, backendGeneration: oldRegistration.backend_generation },
    replacement: { workspaceId: replacement.workspace_id, root: oldRoot },
    relocated: { workspaceId: relocated.workspace_id, root: relocatedRoot, backendGeneration: relocated.backend_generation },
    rejectedWithoutBusinessReads: true, failedHistoryRemoved: true, reopenedOriginalDraft: true };
  result.storedIdentity = await page.evaluate(async histories => {
    const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const serialized = JSON.stringify(histories);
    return { tokenStored: serialized.includes(credential), transportEndpointStored: /https?:\/\//.test(serialized) };
  }, await savedHistory());
  expect(result.storedIdentity).toEqual({ tokenStored: false, transportEndpointStored: false });
  expect(result.searches.filter(read => read.phase !== 'original-input-memory-and-command-history')).toEqual([]);
  expect(result.executions).toEqual([]); expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error instanceof Error ? error.stack : String(error);
  if (page) await page.screenshot({ path: join(dirname(output), 'failure.png') }).catch(cause => { result.screenshotError = String(cause); });
} finally {
  try { await run.close(); }
  catch (cause) { result.cleanupError = String(cause); failure = failure ? new AggregateError([failure, cause], '最近历史验收及关闭均失败') : cause; }
  await writeFile(output, JSON.stringify(result, null, 2) + '\n');
}
if (failure) throw failure;

return result;
}

async function launchChromium({ run, usePage }) {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('最近历史 Chromium', () => browser.close());
  const page = await browser.newPage({ viewport: { width: 1700, height: 1000 } });
  usePage(page);
  const baseUrl = required('BOXTEAM_BROWSER_BASE_URL');
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  const save = () => page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  return { baseUrl, beforeStorageFault: save,
    async reload() { await save(); await page.reload({ waitUntil: 'domcontentloaded' }); },
    async restoreAfterFault() { await page.goto(baseUrl, { waitUntil: 'domcontentloaded' }); },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await runRecentHistory({
    run: TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_recent_history.py'), launch: launchChromium,
  });
}
