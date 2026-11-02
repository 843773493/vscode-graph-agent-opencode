import { expect } from 'playwright/test';

/** 沿原命令、控件、复制与持久化入口验收；不直接写 Input 或模型配置。 */
export async function verifySearchEditors({ page, command, result, fileUri, ownerA, ownerB }) {
  const queryInput = editor => editor.getByRole('textbox', { name: 'Search: Type Search Term and press Enter to search', exact: true });
  const lines = editor => editor.locator('.monaco-editor .view-lines');
  const query = async (editor, value, expected, owner) => {
    const before = result.searches.length;
    await queryInput(editor).fill(value);
    await queryInput(editor).press('Enter');
    await expect.poll(() => result.searches.slice(before).some(request => request.query === value)).toBe(true);
    await expect(lines(editor)).toContainText(expected);
    if (result.searches.slice(before).some(request => request.route !== owner.route)) throw new Error('Search Editor 查询改投其它工作区');
  };

  result.phase = 'search-editor-from-transferred-view';
  await command('search.action.openInEditor');
  const editors = page.locator('.search-editor');
  await expect(editors).toHaveCount(1);
  const a = editors.first();
  await expect(a.locator('.boxteam-search-scope')).toContainText('home');
  await expect(lines(a)).toContainText('dirtyword');
  await query(a, 'quickscope', 'dirtyword quickscope', ownerA);
  await expect(lines(a)).not.toContainText('foreign-buffer');
  await expect(lines(a)).not.toContainText('virtual-buffer');
  // 原扩展必须把已保存的完整位置交给同一文件 Provider，不能拼到 workbench:/Workspace。
  await expect(lines(a)).toContainText(`boxteam-file://${ownerA.gatewayId}/${ownerA.workspaceId}/`);
  result.editorViewScope = true;

  result.phase = 'independent-search-editor-and-prior-config';
  await command('workbench.action.splitEditorRight');
  await expect(editors).toHaveCount(2);
  await command('_workbench.open', await fileUri(ownerB), [undefined, { pinned: true }]);
  await command('search.action.openEditor', { query: 'quickscope', location: 'reuse', triggerSearch: true,
    useExcludeSettingsAndIgnoreFiles: false, contextLines: 0, onlyOpenEditors: false });
  await expect(editors).toHaveCount(2);
  const b = editors.nth(1);
  await expect(b.locator('.boxteam-search-scope')).toContainText('搜索隔离 B');
  await expect(lines(b)).toContainText('foreign-buffer quickscope');
  await expect(lines(b)).not.toContainText('dirtyword');
  // A 复制的旧标签仍留在同组；复用命令只能复用同范围，不能把它改写为 B。
  const groups = page.locator('.editor-group-container');
  await expect(groups.nth(1).locator('.tabs-container .tab').filter({ hasText: 'Search:' })).toHaveCount(2);
  await query(a, 'dirtyword', 'dirtyword', ownerA);
  await query(b, 'foreign-buffer', 'foreign-buffer', ownerB);
  result.editorSplitScopes = true;

  result.phase = 'search-editor-save-as';
  await queryInput(a).click();
  await page.evaluate(() => {
    globalThis.searchEditorSave = { done: false };
    void globalThis.workbench.commands.executeCommand('workbench.action.files.saveAs').then(
      () => { globalThis.searchEditorSave = { done: true }; }, error => { globalThis.searchEditorSave = { done: true, error: String(error) }; });
  });
  const picker = page.locator('.quick-input-widget');
  await expect(picker).toBeVisible();
  await picker.locator('input').fill('/Workspace/saved-A.code-search');
  await picker.locator('input').press('Enter');
  await expect(picker).not.toBeVisible();
  await expect.poll(() => page.evaluate(() => globalThis.searchEditorSave)).toEqual({ done: true });
  const saved = await page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('boxteam-workbench-files', 1);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try {
      const bytes = await new Promise((resolve, reject) => {
        const request = db.transaction('files').objectStore('files').get('/Workspace/saved-A.code-search');
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      return new TextDecoder().decode(bytes);
    } finally { db.close(); }
  });
  const metadata = JSON.parse(saved.split('\n').find(line => line.startsWith('# BoxTeamScope: ')).slice('# BoxTeamScope: '.length));
  expect(metadata.workspaces.map(target => [target.gatewayId, target.workspaceId, target.rootUri])).toEqual([[ownerA.gatewayId, ownerA.workspaceId, ownerA.root]]);
  expect(saved).toContain('dirtyword');
  result.editorSavedScope = true;

  result.phase = 'search-editor-close-reopen-and-reload';
  await command('workbench.action.closeActiveEditor');
  await command('_workbench.open', 'workbench:/Workspace/saved-A.code-search', [undefined, { pinned: true }]);
  await expect(a.locator('.boxteam-search-scope')).toContainText('home');
  await query(a, 'quickscope', 'dirtyword quickscope', ownerA);
  await command('boxteam.flushViewState');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(editors).toHaveCount(2);
  await expect(a.locator('.boxteam-search-scope')).toContainText('home');
  await expect(b.locator('.boxteam-search-scope')).toContainText('搜索隔离 B');
  await query(a, 'dirtyword', 'dirtyword quickscope', ownerA);
  await query(b, 'foreign-buffer', 'foreign-buffer quickscope', ownerB);
  result.editorReloadScopes = true;

  result.phase = 'saved-result-definition-opens-exact-file';
  await a.locator('.view-line').filter({ hasText: 'dirtyword quickscope' }).dblclick();
  const opened = page.locator('.editor-group-container').first().locator('.editor-instance .monaco-editor').filter({ visible: true });
  await expect(opened.locator('.view-lines')).toContainText('dirtyword quickscope');
  await expect(opened.locator('.view-lines')).not.toContainText('foreign-buffer');
  await expect(opened.locator('.view-lines')).not.toContainText('virtual-buffer');
  result.editorDefinitionOwner = true;

  result.phase = 'legacy-results-require-explicit-scope';
  await command('_workbench.open', 'workbench:/Workspace/legacy.code-search', [undefined, { pinned: true }]);
  const legacy = page.locator('.editor-group-container').first().locator('.search-editor');
  await expect(lines(legacy)).toContainText('legacy-display-only');
  await expect(legacy.locator('.boxteam-search-scope')).toContainText('没有明确范围');
  await lines(legacy).click();
  await page.keyboard.press('ControlOrMeta+End');
  await page.keyboard.type('must-not-edit-legacy');
  await expect(lines(legacy)).not.toContainText('must-not-edit-legacy');
  const beforeLegacy = result.searches.length;
  await queryInput(legacy).fill('quickscope');
  await queryInput(legacy).press('Enter');
  await expect(page.locator('.notifications-toasts')).toContainText('历史结果没有明确范围');
  expect(result.searches.slice(beforeLegacy)).toEqual([]);
  await legacy.locator('.boxteam-search-scope').click();
  const scopes = page.locator('.quick-input-widget');
  await expect(scopes).toBeVisible();
  const targetB = scopes.locator('.monaco-list-row').filter({ hasText: '搜索隔离 B' });
  await targetB.getByRole('checkbox').click();
  await scopes.getByRole('button', { name: 'OK', exact: true }).click();
  await expect(legacy.locator('.boxteam-search-scope')).toContainText('搜索隔离 B');
  await expect(lines(legacy)).toContainText('foreign-buffer quickscope');
  await expect(lines(legacy)).not.toContainText('legacy-display-only');
  await expect(lines(legacy)).not.toContainText('dirtyword');
  expect(result.searches.slice(beforeLegacy).length).toBeGreaterThan(0);
  if (result.searches.slice(beforeLegacy).some(request => request.route !== ownerB.route)) throw new Error('旧结果选择范围后仍查询了其它工作区');
  result.editorLegacyScope = true;

  result.phase = 'invalid-scope-rejected-visibly';
  const beforeInvalid = result.searches.length;
  await command('_workbench.open', 'workbench:/Workspace/unknown.code-search', [undefined, { pinned: true }]);
  await expect(page.locator('.editor-group-container').first().locator('.editor-instance')).toContainText('搜索编辑器范围损坏或版本不受支持');
  await command('_workbench.open', 'workbench:/Workspace/broken.code-search', [undefined, { pinned: true }]);
  await expect(page.locator('.editor-group-container').first().locator('.editor-instance')).toContainText('JSON');
  expect(result.searches.slice(beforeInvalid)).toEqual([]);
  result.editorInvalidScope = true;
}
