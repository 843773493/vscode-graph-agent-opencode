import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { expect } from 'playwright/test';
import { writeWorkspaceSettings } from '../../../support/workbench/settings.mjs';

/** 原编辑器并发打开、真实键盘编辑及关闭；不替换 resolver、模型或网络。 */
export async function verifyTextModelSharing({ page, command, api, result, targets, workspace, other }) {
  await command('workbench.action.closeAllEditors');
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off' });
  const name = 'cache-model.txt', path = target => target.rootUri + '/' + name;
  const contentA = await readFile(join(workspace, name), 'utf8');
  const contentB = await readFile(join(other, name), 'utf8');
  const resources = await page.evaluate(targets => targets.map(target => globalThis.workbench.URI.from({
    scheme: 'boxteam-file', authority: target.gatewayId,
    path: `/${target.workspaceId}/${encodeURIComponent(target.rootUri)}/cache-model.txt`,
  }).toString()), targets);
  const reads = target => result.network.filter(item => new URL(item.url).searchParams.get('path') === path(target));
  const open = (resource, column, options = {}) => command('_workbench.open', resource, [column, { pinned: true, ...options }]);
  const groups = page.locator('.editor-group-container');
  const editor = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true });
  const lines = index => editor(index).locator('.view-lines');

  result.phase = 'text-model-concurrent-resolve';
  await command('workbench.action.newGroupRight');
  await expect(groups).toHaveCount(2);
  await Promise.all([open(resources[0], 0), open(resources[0], 1)]);
  for (const index of [0, 1]) await expect(lines(index)).toContainText('A source 01');
  await expect.poll(() => reads(targets[0]).filter(item => item.finished).length).toBe(1);
  expect(reads(targets[0])[0].wireStatus).toBe(200);

  result.phase = 'text-model-shared-edit-independent-selection';
  const dirty = contentA.replace('A source 01', 'A unsaved shared');
  await lines(0).click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText(dirty);
  await command('cursorTop');
  for (const index of [0, 1]) await expect(lines(index)).toContainText('A unsaved shared');
  expect(await readFile(join(workspace, name), 'utf8')).toBe(contentA);
  expect(reads(targets[0])).toHaveLength(1);
  for (const [column, line] of [[0, 3], [1, 8]]) {
    await open(resources[0], column, { selection: { startLineNumber: line, startColumn: 1, endLineNumber: line, endColumn: 12 } });
  }
  const selections = [];
  for (const index of [0, 1]) {
    await command(index ? 'workbench.action.focusSecondEditorGroup' : 'workbench.action.focusFirstEditorGroup');
    await page.keyboard.press('ControlOrMeta+C');
    selections.push(await page.evaluate(() => navigator.clipboard.readText()));
  }
  expect(selections).toEqual(['A source 03', 'A source 08']);

  result.phase = 'text-model-close-one-reference';
  await command('workbench.action.closeActiveEditor');
  await expect(groups).toHaveCount(1);
  await expect(lines(0)).toContainText('A unsaved shared');
  expect(reads(targets[0])).toHaveLength(1);

  result.phase = 'text-model-dirty-external-update';
  const external = contentA.replace('A source 01', 'A external authoritative');
  await writeFile(join(workspace, name), external);
  await command('workbench.action.files.save');
  const conflict = page.getByText(/Failed to save 'cache-model.txt': The content of the file is newer/).first();
  await expect(conflict).toBeVisible();
  result.textModelConflict = { message: await conflict.innerText() };
  await expect(lines(0)).toContainText('A unsaved shared');
  expect(await readFile(join(workspace, name), 'utf8')).toBe(external);
  const current = await api(`/api/v1/workspace/files/content?${new URLSearchParams({ path: path(targets[0]) })}`,
    { headers: { 'X-BoxTeam-Target-Gateway-Id': targets[0].gatewayId, 'X-BoxTeam-Workspace-Id': targets[0].workspaceId } });
  expect(current.status).toBe(200); expect(current.body.data.content).toBe(external);
  // 用户明确放弃本次编辑后，才允许原服务重读和释放最后一个引用。
  await command('workbench.action.files.revert');
  await expect(lines(0)).toContainText('A external authoritative');
  await command('workbench.action.closeAllEditors');
  const beforeReopen = reads(targets[0]).length;
  await open(resources[0], 0);
  await expect(lines(0)).toContainText('A external authoritative');
  await expect.poll(() => reads(targets[0]).length).toBeGreaterThan(beforeReopen);

  result.phase = 'text-model-owner-isolation';
  await command('workbench.action.newGroupRight');
  await open(resources[1], 1);
  await expect(lines(1)).toContainText('B source 01');
  await lines(1).click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('B independent unsaved\n');
  await expect(lines(1)).toContainText('B independent unsaved');
  await expect(lines(0)).toContainText('A external authoritative');
  await expect(lines(0)).not.toContainText('B independent unsaved');
  expect(await readFile(join(other, name), 'utf8')).toBe(contentB);
  result.textModels = { concurrentReads: 1, sharedUnsavedBody: true, selections,
    closeOnePreservesDirty: true, dirtyExternalConflict: true, lastCloseReopensSource: true,
    ownerIsolated: true, reads: targets.map(target => reads(target).length) };
}
