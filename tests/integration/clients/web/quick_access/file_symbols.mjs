import { expect } from 'playwright/test';
import { searchFault } from './query_faults.mjs';

/** 原 JSON 语言服务和编辑器；Gateway 出站装具只延迟真实 Thread 响应。 */
export async function verifyFileSymbols({ page, command, result, fileUri, owner, requests }) {
  result.phase = 'file-symbol-preview-and-restore';
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true });
  const origin = await fileUri(owner, 'symbol-origin.txt');
  const target = await fileUri(owner, 'symbol-target-b.json');
  let failure;
  try {
    await command('_workbench.open', origin, [undefined, { pinned: true }]);
    await expect(editor.locator('.view-lines')).toContainText('symbol origin');
    await editor.locator('.view-lines').click();
    await page.keyboard.press('ControlOrMeta+End');
    await page.keyboard.insertText('unsaved symbol draft');
    await page.keyboard.press('Home');
    const selection = page.locator('[id="status.editor.selection"]');
    await expect(selection).toHaveText('Ln 2, Col 1');
    const originalSelection = await selection.innerText();
    await searchFault(page, 'symbol-target', 'delay', ['thread']);
    await command('workbench.action.quickOpen', 'symbol-target');
    await expect(quick).toContainText('文件 · 本次查询完成');
    await expect(quick).toContainText('Thread · 正在查询');
    const fileRows = quick.locator('[data-quick-input-id^="boxteam-file:"]');
    await expect(fileRows).toHaveCount(2);
    const originalIds = await fileRows.evaluateAll(rows => rows.map(row => row.dataset.quickInputId));
    expect(originalIds[1]).toBe(target);
    const targetRow = quick.getByRole('option').filter({ has: page.locator(`[data-quick-input-id="${target}"]`) });
    await page.keyboard.press('ArrowDown');
    await expect(targetRow).toHaveClass(/focused/);
    const beforeSymbols = requests.length;
    await input.fill('symbol-target:2:3');
    await expect(targetRow).toHaveClass(/focused/);
    await expect(quick).toContainText('Thread · 正在查询');
    const beta = quick.getByRole('option').filter({ hasText: 'beta' });
    await input.fill('symbol-target@beta');
    await expect(beta).toHaveCount(1);
    await expect(beta).toHaveClass(/focused/);
    await expect(editor.locator('.view-lines')).toContainText('owner A');
    await expect(editor.locator('.rangeHighlight')).not.toHaveCount(0);
    expect(requests.length).toBe(beforeSymbols);
    await searchFault(page, 'symbol-target', 'release');
    await expect(beta).toHaveClass(/focused/);

    // 从无匹配符号退回名称列表仍须恢复原列表与第二项，不能再查询或默认选第一项。
    await input.fill('symbol-target@no-such-symbol');
    await expect(quick).toContainText(/No matching editor symbols|没有匹配/);
    await input.fill('symbol-target');
    await expect(targetRow).toHaveClass(/focused/);
    await expect(quick).toContainText('Thread · 没有匹配的资源');
    expect(await fileRows.evaluateAll(rows => rows.map(row => row.dataset.quickInputId))).toEqual(originalIds);
    expect(requests.length).toBe(beforeSymbols);
    await expect(editor.locator('.rangeHighlight')).toHaveCount(0);
    await input.fill('symbol-target@beta');
    await expect(beta).toHaveClass(/focused/);
    await page.keyboard.press('Escape');
    await expect(quick).not.toBeVisible();
    await expect(editor.locator('.view-lines')).toContainText('unsaved symbol draft');
    await expect(selection).toHaveText(originalSelection);
    const originalTab = page.getByRole('tab', { name: /symbol-origin\.txt/ });
    await expect(originalTab).toHaveClass(/dirty/);
    await expect(originalTab).toHaveAttribute('aria-selected', 'true');
    // 原 PickerEditorState 仅关闭仍为 transient 的编辑器；此处核原位置和焦点，不强关已保留的 Tab。
    result.fileSymbolCancel = { originalIds, target, zeroAdditionalSearch: true, originalSelection, dirtyPreserved: true,
      retainedTargetTabs: await page.getByRole('tab', { name: /symbol-target-b\.json/ }).count() };

    result.phase = 'file-symbol-accept';
    await command('workbench.action.quickOpen', 'symbol-target-b');
    await expect(quick).toContainText('文件 · 本次查询完成');
    await input.fill('symbol-target-b@beta');
    await expect(beta).toHaveClass(/focused/);
    await page.keyboard.press('Enter');
    await expect(quick).not.toBeVisible();
    await expect(editor.locator('.view-lines')).toContainText('owner A');
    await expect(selection).toContainText('Ln 3, Col 3');
    await expect(editor.locator('.rangeHighlight')).toHaveCount(0);
    result.fileSymbolAccept = { target, position: '3:3', keyboardAccepted: true };

    result.phase = 'original-text-navigation-prefixes';
    const beforeNative = requests.length;
    await command('workbench.action.quickOpen', ':2:4');
    await expect(quick).toBeVisible();
    await page.keyboard.press('Enter');
    await expect(selection).toHaveText('Ln 2, Col 4');
    await command('workbench.action.quickOpen', '::3');
    await expect(quick).toContainText(/line 2 at column 1|第 2 行/);
    await page.keyboard.press('Enter');
    await expect(selection).toHaveText('Ln 2, Col 1');
    await command('workbench.action.quickOpen', '@beta');
    await expect(beta).toHaveCount(1);
    await page.keyboard.press('Enter');
    await expect(selection).toHaveText('Ln 3, Col 3');
    await command('workbench.action.quickOpen', ':2:4');
    await page.keyboard.press('Enter');
    await expect(selection).toHaveText('Ln 2, Col 4');
    await command('workbench.action.quickOpen', '@:beta');
    await expect(beta).toHaveCount(1);
    await expect(editor.locator('.rangeHighlight')).not.toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(selection).toHaveText('Ln 2, Col 4');
    await expect(editor.locator('.rangeHighlight')).toHaveCount(0);
    expect(requests.length).toBe(beforeNative);
    result.originalTextNavigation = { lineColumn: '2:4', offset: '2:1', symbol: '3:3', categoryCancel: '2:4', zeroSearchRequests: true };
  } catch (error) {
    failure = error;
    result.fileSymbolError = String(error?.stack ?? error);
  } finally {
    // 本场景创建的草稿由真实产品命令丢弃；清理错误与原断言错误一并保留。
    try {
      await searchFault(page, 'symbol-target', 'release');
      await command('_workbench.open', origin, [undefined, { pinned: true }]);
      await command('workbench.action.revertAndCloseActiveEditor');
    } catch (cleanupError) {
      if (failure) throw new AggregateError([failure, cleanupError], '符号场景及草稿清理均失败');
      throw cleanupError;
    }
  }
  if (failure) throw failure;
}
