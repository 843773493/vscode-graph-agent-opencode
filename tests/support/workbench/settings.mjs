import { expect } from 'playwright/test';

/** 只读实际存储，用于核对产品保存结果，不替设置服务写入。 */
export async function readSavedSettings(page) {
  return page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('boxteam-workbench-files', 1);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction('files'), request = transaction.objectStore('files').openCursor(), settings = {};
        request.onsuccess = () => {
          const cursor = request.result;
          if (!cursor) return;
          if (String(cursor.key).endsWith('settings.json')) settings[cursor.key] = new TextDecoder().decode(cursor.value);
          cursor.continue();
        };
        transaction.oncomplete = () => resolve(settings);
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
      });
    } finally { db.close(); }
  });
}

/** 整份 JSON 经真实粘贴与保存，避免把逐键自动补括号产生的无效文件当成成功。 */
export async function writeWorkspaceSettings({ page, command }, settings) {
  await command('workbench.action.openWorkspaceSettingsFile');
  const dialog = page.getByRole('dialog', { name: /settings\.json$/ });
  const body = dialog.locator('.monaco-editor [role="textbox"]');
  await body.focus();
  await body.press('ControlOrMeta+A');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(text => navigator.clipboard.writeText(text), JSON.stringify(settings, null, 2) + '\n');
  await body.press('ControlOrMeta+V');
  await expect(dialog.locator('.view-lines')).toContainText(Object.keys(settings).at(-1));
  await command('workbench.action.files.save');
  await expect.poll(async () => JSON.parse((await readSavedSettings(page))['/Workspace/.vscode/settings.json'])).toEqual(settings);
  await command('workbench.action.closeActiveEditor');
  await expect(dialog).not.toBeVisible();
}
