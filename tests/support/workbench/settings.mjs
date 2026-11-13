import { expect } from 'playwright/test';

/** 只读实际存储，用于核对产品保存结果，不替设置服务写入。 */
export async function readSavedSettings(page, database = { name: 'boxteam-workbench-files', version: 1, store: 'files' }) {
  return page.evaluate(async database => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open(database.name, database.version);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction(database.store), request = transaction.objectStore(database.store).openCursor(), settings = {};
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
  }, database);
}

/** 整份 JSON 经真实粘贴与保存，避免把逐键自动补括号产生的无效文件当成成功。 */
async function writeSettingsFile({ page, command }, settings, target) {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  const payload = JSON.stringify(settings, null, 2) + '\n';
  await page.evaluate(text => navigator.clipboard.writeText(text), payload);
  await command(target.command);
  const dialog = page.getByRole('dialog', { name: /settings\.json$/ });
  await expect(dialog).toBeVisible();
  await dialog.locator('.view-lines').click();
  await command('editor.action.selectAll');
  await page.keyboard.press('ControlOrMeta+V');
  await command('workbench.action.files.save');
  await expect.poll(async () => JSON.parse((await readSavedSettings(page, target.database))[target.key])).toEqual(settings);
  await command('workbench.action.closeActiveEditor');
  await expect(dialog).not.toBeVisible();
}

export function writeWorkspaceSettings(context, settings) {
  return writeSettingsFile(context, settings, { command: 'workbench.action.openWorkspaceSettingsFile', key: '/Workspace/.vscode/settings.json' });
}

/** Application scope 只能沿用户设置生效，不能写入工作区后把保存成功当成已应用。 */
export function writeUserSettings(context, settings) {
  return writeSettingsFile(context, settings, { command: 'workbench.action.openSettingsJson', key: '/User/settings.json',
    database: { name: 'vscode-web-db', version: 3, store: 'vscode-userdata-store' } });
}
