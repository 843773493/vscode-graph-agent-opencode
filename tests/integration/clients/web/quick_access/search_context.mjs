import { expect } from 'playwright/test';
import { writeWorkspaceSettings } from './settings.mjs';

/** 原控件与 Gateway 请求共同证明上下文来源，以及 rawData 的自动重查分支。 */
export async function verifySearchContext({ page, command, result, fileUri, owner }) {
  result.phase = 'dirty-model-context';
  await command('_workbench.open', await fileUri(owner), [undefined, { pinned: true }]);
  const group = page.locator('.editor-group-container').first();
  const text = group.locator('.editor-instance .monaco-editor').filter({ visible: true });
  await expect(text.locator('.view-lines')).toContainText('dirtyword');
  await text.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('dirty context before\ndirtyword quickscope 🧪\ndirty context after');
  await command('search.action.openEditor', { query: 'dirtyword', location: 'new', triggerSearch: true,
    contextLines: 1, useExcludeSettingsAndIgnoreFiles: false, onlyOpenEditors: false });
  const editor = group.locator('.search-editor');
  const lines = editor.locator('.monaco-editor .view-lines');
  await expect(lines).toContainText('dirty context before');
  await expect(lines).toContainText('dirty context after');
  await expect(lines).not.toContainText('diskword');
  result.editorDirtyContext = true;

  result.phase = 'enable-default-context-through-original-settings';
  await writeWorkspaceSettings({ page, command }, { 'search.useIgnoreFiles': false, 'files.autoSave': 'off',
    'search.searchEditor.defaultNumberOfContextLines': 1 });

  result.phase = 'clean-view-to-editor-automatic-context-rerun';
  // 恢复的视图在初始化后首次查询，应捕获此刻明确的 A；不能记住启动早期的无范围错误。
  await command('_workbench.open', await fileUri(owner), [undefined, { pinned: true }]);
  await command('workbench.action.findInFiles', { query: 'contextbranchonly', triggerSearch: true,
    onlyOpenEditors: false, useExcludeSettingsAndIgnoreFiles: false });
  const view = page.locator('.search-view');
  await expect(view.getByRole('tree')).toHaveAttribute('aria-label', /Search: contextbranchonly/);
  await expect(view).toContainText('contextbranchonly');
  const before = result.searches.length;
  await command('search.action.openInEditor');
  await expect.poll(() => result.searches.slice(before).some(request => request.query === 'contextbranchonly'
    && request.search_options.context_lines === 1)).toBe(true);
  await expect(lines).toContainText('A context before');
  await expect(lines).toContainText('A context after');
  await expect(lines).not.toContainText('B context');
  await expect(editor.locator('.boxteam-search-scope')).toContainText('home');
  const requests = result.searches.slice(before);
  expect(requests.length).toBeGreaterThan(0);
  if (requests.some(request => request.workspaceId !== owner.workspaceId || request.gatewayId !== owner.gatewayId || request.uri !== owner.root)) throw new Error('上下文自动重查改投其它范围');
  // 相邻命中相互成为上下文时，原 serializer 仍只输出一份命中行。
  expect(await lines.locator('.view-line').filter({ hasText: 'contextbranchonly' }).count()).toBe(2);
  result.editorAutomaticContextRerun = true;
}
