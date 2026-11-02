import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { expect } from 'playwright/test';

/** 外部真实文件变化经原 watcher／Gateway 更新；空闲不轮询正文，dirty 草稿仍受冲突保护。 */
export async function verifyFileWatch({ page, command, result, workspace, resource, owner }) {
  result.phase = 'shared-file-watcher';
  const path = owner.root + '/watch-contract.txt', reads = [], saves = [];
  let lastRead = Date.now();
  const observe = response => {
    const request = response.request(), url = new URL(request.url());
    if (url.pathname !== '/api/v1/workspace/files/content' || url.searchParams.get('path') !== path) return;
    const receipt = { route: request.headers()['x-boxteam-workspace-id'], status: response.status(), path };
    if (request.method() === 'GET') { reads.push(receipt); lastRead = Date.now(); }
    if (request.method() === 'PUT') saves.push(receipt);
  };
  page.on('response', observe);
  try {
    await command('_workbench.open', resource, [undefined, { pinned: true }]);
    const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true });
    await expect(editor.locator('.view-lines')).toContainText('watch initial');
    await expect.poll(() => Date.now() - lastRead > 500).toBe(true);
    const before = reads.length;
    // 超过原五秒周期，真实 HTTP 中没有无变化正文读取。
    await page.waitForTimeout(6000);
    const idleContentReads = reads.length - before;
    expect(idleContentReads).toBe(0);
    await writeFile(join(workspace, 'watch-contract.txt'), 'watch external Ω\n');
    await expect(editor.locator('.view-lines')).toContainText('watch external Ω');
    expect(reads.length).toBeGreaterThan(before);
    expect(reads.every(receipt => receipt.route === owner.route && receipt.status === 200)).toBe(true);

    const body = editor.locator('[role="textbox"]');
    await body.focus();
    await body.press('ControlOrMeta+A');
    await page.keyboard.insertText('watch dirty must survive');
    await expect(editor.locator('.view-lines')).toContainText('watch dirty must survive');
    await writeFile(join(workspace, 'watch-contract.txt'), 'watch newer external body\n');
    await command('workbench.action.files.save');
    await expect(page.locator('.notifications-toasts')).toContainText(/newer|后端文件已变化/);
    await expect(editor.locator('.view-lines')).toContainText('watch dirty must survive');
    expect(await readFile(join(workspace, 'watch-contract.txt'), 'utf8')).toBe('watch newer external body\n');
    expect(saves.every(receipt => receipt.route === owner.route && receipt.status === 409)).toBe(true);
    result.fileWatch = { idleContentReads, externalUpdate: true, dirtyConflict: true,
      reads, saves, conflictOwner: saves.length ? 'backend-cas' : 'original-file-service' };
    await command('workbench.action.files.revert');
    await expect(editor.locator('.view-lines')).toContainText('watch newer external body');
    await command('workbench.action.closeActiveEditor');
  } finally { page.off('response', observe); }
}
