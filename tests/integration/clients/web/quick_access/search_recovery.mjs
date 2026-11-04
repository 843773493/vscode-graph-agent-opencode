import { rename } from 'node:fs/promises';
import { expect } from 'playwright/test';
import { searchFault } from './query_faults.mjs';

/** 只读检查原工作副本 backup；内容与配置均由原编辑器生成。 */
async function readBackups(page) {
  return page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('vscode-web-db', 3);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try {
      const store = db.transaction('vscode-userdata-store').objectStore('vscode-userdata-store');
      const read = request => new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      const [keys, values] = await Promise.all([read(store.getAllKeys()), read(store.getAll())]);
      return keys.flatMap((key, index) => String(key).includes('/Backups/') && String(key).includes('/search-editor/')
        ? [{ key, text: new TextDecoder().decode(values[index]) }] : []);
    } finally { db.close(); }
  });
}

/** 真实备份、迟到 Pane 及工作区搬迁；不改写 Input 或伪造业务回执。 */
export async function verifySearchRecovery({ page, command, api, result, fileUri, ownerA, ownerB, oldRoot, replacementRoot }) {
  const groups = page.locator('.editor-group-container');
  const a = groups.first().locator('.search-editor'), b = groups.nth(1).locator('.search-editor');
  const lines = editor => editor.locator('.monaco-editor .view-lines');
  const input = editor => editor.getByRole('textbox', { name: 'Search: Type Search Term and press Enter to search', exact: true });
  const rerun = async (editor, query, expected, owner) => {
    const before = result.searches.length;
    await input(editor).fill(query);
    await input(editor).press('Enter');
    await expect.poll(() => result.searches.slice(before).some(request => request.query === query)).toBe(true);
    await expect(lines(editor)).toContainText(expected);
    const requests = result.searches.slice(before);
    expect(requests.every(request => request.workspaceId === owner.workspaceId && request.gatewayId === owner.gatewayId && request.uri === owner.root)).toBe(true);
  };

  result.phase = 'unsaved-search-result-backups';
  for (const [editor, marker] of [[a, 'backup-only-A 🧪'], [b, 'backup-only-B 🧪']]) {
    const originalQuery = await input(editor).inputValue();
    const body = editor.locator('.monaco-editor [role="textbox"]');
    await body.focus();
    await expect(body).toBeFocused();
    await body.press('ControlOrMeta+End');
    await body.press('Enter');
    await body.pressSequentially(marker);
    await expect(input(editor)).toHaveValue(originalQuery);
    await expect(lines(editor)).toContainText(marker);
  }
  let backups;
  await expect.poll(async () => {
    backups = await readBackups(page);
    return ['backup-only-A 🧪', 'backup-only-B 🧪'].every(marker => backups.some(backup => backup.text.includes(marker)));
  }).toBe(true);
  result.editorBackupFacts = [ownerA, ownerB].map((owner, index) => {
    const backup = backups.find(backup => backup.text.includes(`backup-only-${index === 0 ? 'A' : 'B'} 🧪`));
    const metadata = JSON.parse(backup.text.split('\n').find(line => line.startsWith('# BoxTeamScope: ')).slice('# BoxTeamScope: '.length));
    expect(metadata.workspaces.map(target => [target.gatewayId, target.workspaceId, target.rootUri])).toEqual([
      [owner.gatewayId, owner.workspaceId, owner.root],
    ]);
    return { key: backup.key, scope: metadata };
  });
  await command('boxteam.flushViewState');
  const beforeRestore = result.searches.length;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(a.locator('.boxteam-search-scope')).toContainText('home');
  await expect(b.locator('.boxteam-search-scope')).toContainText('搜索隔离 B');
  await expect(lines(a)).toContainText('backup-only-A 🧪');
  await expect(lines(b)).toContainText('backup-only-B 🧪');
  // 未保存的自定义正文仅存在原 backup，恢复不能靠重新搜索生成。
  expect(result.searches.slice(beforeRestore)).toEqual([]);
  result.editorDeepBackupRecovery = true;
  await rerun(a, 'dirtyword', 'dirtyword quickscope', ownerA);
  await rerun(b, 'foreign-buffer', 'foreign-buffer quickscope', ownerB);

  result.phase = 'late-search-pane-does-not-rebind-current-input';
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let delayed = false;
  const pattern = '**/api/gateway/workspaces?check_health=false';
  const delay = async route => {
    const response = await route.fetch();
    if (!delayed) { delayed = true; await gate; }
    await route.fulfill({ response });
  };
  await page.route(pattern, delay);
  try {
    // 已保存 A 在 B 所在组重开；只延迟真实控制面回执，保留其正文和请求身份。
    await page.evaluate(() => {
      globalThis.lateSearchOpen = { done: false };
      void globalThis.workbench.commands.executeCommand('_workbench.open', 'workbench:/Workspace/saved-A.code-search',
        [1, { pinned: true }]).then(() => { globalThis.lateSearchOpen = { done: true }; },
        error => { globalThis.lateSearchOpen = { done: true, error: String(error) }; });
    });
    await expect.poll(() => delayed).toBe(true);
    await command('_workbench.open', await fileUri(ownerB), [1, { pinned: true }]);
    await command('search.action.openEditor', { query: 'foreign-buffer', location: 'reuse', triggerSearch: true,
      contextLines: 0, useExcludeSettingsAndIgnoreFiles: false, onlyOpenEditors: false });
    await expect(b.locator('.boxteam-search-scope')).toContainText('搜索隔离 B');
    await expect(lines(b)).toContainText('foreign-buffer quickscope');
    release();
    await expect.poll(() => page.evaluate(() => globalThis.lateSearchOpen)).toEqual({ done: true });
    await rerun(b, 'quickscope', 'foreign-buffer quickscope', ownerB);
    await expect(lines(b)).not.toContainText('dirtyword');
    result.editorLatePaneIsolation = true;
  } finally { release(); await page.unroute(pattern, delay); }

  result.phase = 'original-path-reused-by-different-owner';
  // 停止并移除原登记后搬迁；原路径上的新工作区不得继承 B 的 UUID。
  const registered = (await api('/api/gateway/workspaces?check_health=false')).data.items.find(target => target.workspace_id === ownerB.workspaceId && target.gateway_id === ownerB.gatewayId);
  await api(`/api/gateway/managed-workspaces/${ownerB.workspaceId}?force=true`, { method: 'DELETE', body: JSON.stringify({
    expected_backend_generation: registered.backend_generation, expected_workspace_ids: registered.affected_workspace_ids,
  }) });
  const relocatedRoot = oldRoot + '-relocated';
  await rename(oldRoot, relocatedRoot);
  await rename(replacementRoot, oldRoot);
  await api('/api/gateway/managed-workspaces', { method: 'POST',
    body: JSON.stringify({ root_path: oldRoot, name: '替代工作区 C' }) });
  // 管理回执只给注册摘要，稳定身份必须从正式工作区 DTO 取得。
  const replacement = (await api('/api/gateway/workspaces?check_health=false')).data.items.find(target => target.name === '替代工作区 C');
  expect(replacement.gateway_id).toBe(ownerB.gatewayId);
  expect(replacement.workspace_id).toEqual(expect.any(String));
  expect(replacement.workspace_id).not.toBe(ownerB.workspaceId);
  const beforeMissing = result.searches.length;
  // 只在出站边界观察：失效 owner 应在分发前被拒绝，不能查询复用原路径的 C。
  await searchFault(page, 'quickscope', 'fail', ['file']);
  const rejectedResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/search'
    && response.request().postDataJSON()?.queries.some(query => query.category === 'file' && query.query === 'quickscope'));
  await input(b).fill('quickscope');
  await input(b).press('Enter');
  const rejected = await rejectedResponse;
  expect(rejected.status()).toBe(404);
  const error = await rejected.json(), message = `工作区归属未登记或已移除: ${ownerB.gatewayId}/${ownerB.workspaceId}`;
  expect(error.detail.code).toBe('owner_unavailable');
  expect(error.detail.message).toBe(message);
  expect(error.request_id).toBeTruthy();
  expect(error.request_id).toBe(rejected.headers()['x-request-id']);
  await expect(page.locator('.notifications-toasts')).toContainText(message);
  const missingRequests = result.searches.slice(beforeMissing);
  expect(missingRequests.length).toBeGreaterThan(0);
  expect(missingRequests.every(request => request.workspaceId === ownerB.workspaceId && request.gatewayId === ownerB.gatewayId && request.uri === ownerB.root)).toBe(true);
  expect((await searchFault(page, 'quickscope')).reads).toEqual([]);
  await searchFault(page, 'quickscope', 'release');
  result.editorRejectedOwner = { gatewayId: ownerB.gatewayId, workspaceId: ownerB.workspaceId,
    status: rejected.status(), requestId: error.request_id, dispatched: 0 };

  result.phase = 'stable-owner-resolves-relocated-root';
  await api('/api/gateway/managed-workspaces', { method: 'POST',
    body: JSON.stringify({ root_path: relocatedRoot, name: '迁移后的搜索隔离 B' }) });
  const relocated = (await api('/api/gateway/workspaces?check_health=false')).data.items.find(target => target.name === '迁移后的搜索隔离 B');
  expect(relocated.gateway_id).toBe(ownerB.gatewayId);
  expect(relocated.workspace_id).toBe(ownerB.workspaceId);
  await rerun(b, 'quickscope', 'foreign-buffer quickscope', ownerB);
  await expect(lines(b)).not.toContainText('replacement-C');
  await command('boxteam.flushViewState');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(b.locator('.boxteam-search-scope')).toContainText('迁移后的搜索隔离 B');
  await rerun(a, 'dirtyword', 'dirtyword quickscope', ownerA);
  await rerun(b, 'quickscope', 'foreign-buffer quickscope', ownerB);
  await expect(lines(b)).not.toContainText('replacement-C');
  result.editorOwnerRecovery = { original: ownerB.workspaceId, replacement: replacement.workspace_id,
    current: relocated.workspace_id, gatewayId: relocated.gateway_id };
}
