import { expect } from 'playwright/test';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

/** 使用产品已生成的 Thread／文件／Diff，核对原版编辑器导航及关闭行为。 */
export async function verifyEditorNavigation({ page, command, owner, fileUri, result }) {
  const groups = page.locator('.editor-group-container');
  const quick = page.locator('.quick-input-widget');
  const diffs = () => quick.getByRole('option', { name: /diff-history\.txt/ });
  const activeGroup = index => expect(groups.nth(index)).toHaveClass(/\bactive\b/);
  const searchReads = [];
  const observe = request => {
    if (new URL(request.url()).pathname.endsWith('/search')) searchReads.push(request.url());
  };
  page.on('request', observe);
  try {
    result.phase = 'native-editor-navigation';
    // 关闭只读来源标签，原组内仍保留真实 Diff。
    await command('workbench.action.closeActiveEditor');
    await expect(groups.nth(1).locator('.monaco-diff-editor').filter({ visible: true })).toHaveCount(1);
    await command('workbench.action.quickOpen', 'edt active ');
    await expect(diffs()).toHaveCount(1);
    await expect(quick).not.toContainText(owner.title);
    await page.keyboard.press('Escape');

    await command('workbench.action.quickOpen', 'edt ');
    await expect(diffs()).toHaveCount(2);
    await expect(quick.getByRole('option', { name: new RegExp(owner.threadId) })).toHaveCount(1);
    const firstDiff = quick.getByRole('option', { name: /diff-history\.txt.*Group 1/ });
    const secondDiff = quick.getByRole('option', { name: /diff-history\.txt.*Group 2/ });
    await expect(firstDiff).toHaveCount(1); await expect(secondDiff).toHaveCount(1);
    await firstDiff.click();
    await activeGroup(0);
    await expect(groups.nth(0).locator('.monaco-diff-editor').filter({ visible: true })).toHaveCount(1);

    await command('workbench.action.quickOpen', 'edt mru ');
    await expect(diffs()).toHaveCount(2);
    await expect(diffs().first()).toHaveClass(/focused/);
    await expect(diffs().first()).toHaveAttribute('aria-label', /Group 1/);
    await secondDiff.click();
    await activeGroup(1);
    await expect(groups.nth(1).locator('.monaco-diff-editor').filter({ visible: true })).toHaveCount(1);
    result.editorGroupAndMru = true;

    result.phase = 'native-editor-dirty-close';
    await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off' });
    await command('_workbench.open', fileUri);
    const editableTab = groups.nth(1).getByRole('tab', { name: /^diff-history\.txt(?:,|$)/ });
    await expect(editableTab).toBeVisible();
    const editor = groups.nth(1).locator('.editor-instance .monaco-editor').filter({ visible: true });
    await expect(editor.locator('.view-lines')).toContainText('history v2 line 01');
    await editor.getByRole('textbox').focus();
    await page.keyboard.press('ControlOrMeta+Home');
    await page.keyboard.insertText('DIRTY_EDITOR_PROBE\n');
    await expect(editor.locator('.view-lines')).toContainText('DIRTY_EDITOR_PROBE');
    await command('workbench.action.quickOpen', 'edt active diff-history.txt');
    await expect(diffs()).toHaveCount(2);
    const dirty = quick.getByRole('option', { name: /unsaved changes/ });
    await expect(dirty).toHaveCount(1);
    await dirty.getByRole('button', { name: 'Close Editor', exact: true }).click();
    const discard = page.getByRole('button', { name: "Don't Save", exact: true });
    await expect(discard).toBeVisible(); await discard.click();
    await expect(editableTab).toHaveCount(0);
    // 关闭未保存文件必须经原确认；另一组 Diff 及磁盘原件仍由产品原 owner 保持。
    await command('workbench.action.quickOpen', 'edt active diff-history.txt');
    await expect(diffs()).toHaveCount(1);
    await expect(quick.getByRole('option', { name: /unsaved changes/ })).toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(quick).not.toBeVisible();
    await expect(groups.nth(0).locator('.monaco-diff-editor').filter({ visible: true })).toHaveCount(1);
    await expect(groups.nth(1).locator('.monaco-diff-editor .editor.modified').getByRole('textbox')).toBeFocused();
    result.editorDirtyClose = true;

    result.phase = 'native-editor-close-one-copy';
    await command('workbench.action.quickOpen', 'edt diff-history.txt');
    await expect(diffs()).toHaveCount(2);
    await expect(firstDiff).toHaveClass(/focused/);
    await firstDiff.getByRole('button', { name: 'Close Editor', exact: true }).click();
    await expect(diffs()).toHaveCount(1);
    await expect(diffs().first()).toHaveAttribute('aria-label', /Group 2/);
    await page.keyboard.press('Escape');
    await expect(groups.nth(0).locator(`section.boxteam-thread[data-thread-id="${owner.threadId}"] textarea`)).toBeEnabled({ timeout: 30_000 });
    await expect(groups.nth(1).locator('.monaco-diff-editor').filter({ visible: true })).toHaveCount(1);
    expect(searchReads).toEqual([]);
    result.editorCloseIsolated = true;
  } finally { page.off('request', observe); }
}

/** 原 view Provider 发现项目已有视图；面板仍属于当前工作区的底部区域。 */
export async function verifyViewNavigation({ page, command, owner, result }) {
  result.phase = 'native-view-navigation';
  const quick = page.locator('.quick-input-widget');
  // 原候选的可访问名称还包含容器描述，按精确可见标签定位视图。
  const view = label => quick.getByRole('option').filter({ has: page.getByText(label, { exact: true }) }).first();
  // 原生 JSON 草稿有真实符号服务，用它验证从 Thread 返回文本后的原 @／@: 分支。
  await command('_workbench.open', 'untitled:quick-access-outline.json');
  const editor = page.locator('.editor-group-container.active .editor-instance .monaco-editor').filter({ visible: true });
  await expect(editor).toHaveCount(1);
  await editor.getByRole('textbox').focus();
  await page.keyboard.insertText('{"alpha":1,"beta":2}');
  await command('workbench.action.quickOpen', '@beta');
  await expect(quick.getByRole('option', { name: /beta/ })).toHaveCount(1);
  await page.keyboard.press('Enter');
  const selection = page.locator('[id="status.editor.selection"]');
  await expect(selection).toHaveText('Ln 1, Col 12');
  await command('workbench.action.quickOpen', '@:alpha');
  await expect(quick.getByRole('option', { name: /alpha/ })).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(selection).toHaveText('Ln 1, Col 12');
  await command('workbench.action.revertAndCloseActiveEditor');
  result.textSymbolsAfterThread = true;
  await command('workbench.action.quickOpen', 'view 会话');
  await view('会话').click();
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible();
  const panels = [
    ['工作区终端', 'terminal'], ['工作区输出', 'output'], ['自动化', 'automation'],
    ['端口', 'ports'], ['MCP 连接', 'mcp'], ['浏览器', 'browser'],
  ];
  const visited = [];
  for (const [label, kind] of panels) {
    await command('workbench.action.quickOpen', `view ${label}`);
    // 单视图容器与同名视图都由原 Provider 提供，二者均聚焦相同产品视图。
    await view(label).click();
    const panel = page.locator('.part.panel').locator(`.native-workspace-panel[data-panel-kind="${kind}"][data-workspace-id="${owner.workspaceId}"]`);
    await expect(panel).toBeVisible();
    const content = panel.locator('.native-workspace-panel-content:not([hidden])');
    await expect(content).toBeVisible();
    await expect(content).not.toContainText('正在确认工作区…');
    await expect(panel.getByRole('alert')).toHaveCount(0);
    visited.push(kind);
  }
  result.existingWorkspaceViews = visited;
}
