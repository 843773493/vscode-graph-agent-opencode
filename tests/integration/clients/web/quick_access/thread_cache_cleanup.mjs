// Integration：由真实执行生成历史，再经正式工作台清理镜像；只读 IndexedDB 核对结果。
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { readSavedSettings, writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`历史缓存验收缺少 ${name}`); return value; };
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_recent_history.py');
const identities = JSON.parse(required('BOXTEAM_CACHE_THREADS'));
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], forbiddenWrites: [],
  boundary: '真实 Gateway、产品执行、model_stream 精确回放及正式 Workbench；不预写历史、草稿或缓存，不替换响应。' };
let page, failure;
const checkpoint = async phase => { result.phase = phase; await writeFile(output, JSON.stringify(result, null, 2) + '\n'); };
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('历史缓存 Chromium', () => browser.close());
  page = await browser.newPage({ viewport: { width: 1700, height: 1000 } });
  page.on('pageerror', cause => result.pageErrors.push(String(cause)));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && (/\/(messages|jobs)(\/|$)/.test(path)
      || /session-catalog\/operations:enqueue|\/config(?:\/|$)|\/workspaces\/[^/]+\/(activate|reconnect|runtime\/)/.test(path))) {
      result.forbiddenWrites.push({ phase: result.phase, method: request.method(), path });
    }
  });
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const ready = () => expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const pane = target => page.locator(`section.boxteam-thread[data-workspace-id="${target.workspace_id}"]`
    + `[data-session-id="${target.session_id}"][data-thread-id="${target.thread_id}"]`);
  const resource = target => `boxteam-thread://${target.gateway_id}/${target.workspace_id}/${target.session_id}/${target.thread_id}`;
  const cacheKey = target => JSON.stringify([target.gateway_id, 'thread-history', target.workspace_id, target.session_id, target.thread_id]);
  const views = () => page.evaluate(async () => {
    const read = request => new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    const db = await read(indexedDB.open('boxteam-browser-views'));
    try {
      const store = db.transaction('views').objectStore('views');
      const [keys, values] = await Promise.all([read(store.getAllKeys()), read(store.getAll())]);
      return Object.fromEntries(keys.map((key, index) => [key, values[index]]));
    } finally { db.close(); }
  });
  // 只读原 WorkingCopyBackup／History 的实际产物，不代替编辑器生成恢复状态。
  const recoveryRecords = () => page.evaluate(async () => {
    const read = request => new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    const db = await read(indexedDB.open('vscode-web-db'));
    try {
      const store = db.transaction('vscode-userdata-store').objectStore('vscode-userdata-store');
      const [keys, values] = await Promise.all([read(store.getAllKeys()), read(store.getAll())]);
      return keys.flatMap((key, index) => /\/(History|Backups)\//.test(String(key))
        ? [{ key: String(key), text: new TextDecoder().decode(values[index]) }] : []);
    } finally { db.close(); }
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' }); await ready();
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off', 'editor.fontSize': 17 });
  const savedSettings = await readSavedSettings(page);
  const quick = page.locator('.quick-input-widget');
  const open = async target => {
    const uri = `boxteam://${target.gateway_id}/workspace/${target.workspace_id}/resources/session/${target.session_id}/threads/${target.thread_id}`;
    await command('workbench.action.quickOpen', uri);
    await quick.locator(`[data-quick-input-id="link:${uri}"]`).click();
    await expect(pane(target)).toContainText(target.answer);
    await expect(pane(target).getByTestId('composer-input')).toBeVisible();
  };
  const [a, b] = identities;
  for (const [index, target] of identities.entries()) {
    await open(target); await pane(target).getByTestId('composer-input').fill(`保留草稿 ${index}`);
  }
  await checkpoint('product-file-save-local-history-and-dirty-backup');
  const fileUri = await page.evaluate(({ target, root }) => globalThis.workbench.URI.from({
    scheme: 'boxteam-file', authority: target.gateway_id,
    path: `/${target.workspace_id}/${encodeURIComponent(root)}/cache-protected.txt`,
  }).toString(), { target: a, root: required('BOXTEAM_CACHE_FILE_ROOT_URI') });
  await command('_workbench.open', fileUri, [undefined, { pinned: true }]);
  const groups = page.locator('.editor-group-container');
  const editor = group => group.locator('.editor-instance .monaco-editor').filter({ visible: true });
  // Go To 接受可视列；ASCII 行让下方的列断言直接对应模型列，避免全角字符的宽度换算。
  const savedBody = '已保存的历史正文\n' + Array.from({ length: 160 }, (_, index) => `recovery line ${index + 1}`).join('\n');
  const dirtyBody = savedBody.replace('已保存的历史正文', '尚未保存的正文');
  const replaceText = async text => {
    const input = editor(groups.first()).getByRole('textbox');
    await input.focus(); await input.press('ControlOrMeta+A');
    await page.evaluate(text => navigator.clipboard.writeText(text), text);
    await input.press('ControlOrMeta+V');
  };
  await replaceText(savedBody);
  const saved = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname.endsWith('/workspace/files/content'));
  await command('workbench.action.files.save');
  expect((await saved).status()).toBe(200);
  const historyListing = records => records.filter(record => record.key.endsWith('/entries.json'))
    .map(record => ({ ...record, data: JSON.parse(record.text) })).find(record => record.data.resource === fileUri);
  await expect.poll(async () => !!historyListing(await recoveryRecords())?.data.entries.length).toBe(true);
  expect((await recoveryRecords()).some(record => record.text === savedBody)).toBe(true);
  result.automaticLocalHistory = true;
  await command('workbench.action.localHistory.create');
  await expect(quick).toBeVisible();
  await quick.locator('input').first().fill('清理后仍保留的文件历史'); await page.keyboard.press('Enter');
  await expect.poll(async () => historyListing(await recoveryRecords())?.data.entries.some(entry => entry.source === '清理后仍保留的文件历史')).toBe(true);
  result.manualLocalHistory = true;
  await command('timeline.focus');
  await expect(page.locator('.timeline-tree-view')).toContainText('清理后仍保留的文件历史');
  result.timelineOwnerPreserved = true;
  await command('workbench.action.focusFirstEditorGroup');
  await replaceText(dirtyBody);
  await expect.poll(async () => (await recoveryRecords()).some(record => record.key.includes('/Backups/')
    && record.text.includes(fileUri) && record.text.endsWith(dirtyBody))).toBe(true);
  const protectedHistory = (await recoveryRecords()).filter(record => record.key.includes('/History/'));
  await command('workbench.action.splitEditorRight');
  const editorViewMetrics = async index => page.evaluate(index => {
    const groups = [...document.querySelectorAll('.editor-group-container')];
    const root = groups[index]?.querySelector('.editor-instance .monaco-editor');
    const viewport = root?.querySelector('.overflow-guard')?.getBoundingClientRect();
    const visibleNumbers = [...(root?.querySelectorAll('.line-numbers') ?? [])].map(node => {
      const bounds = node.getBoundingClientRect();
      return { text: node.textContent, top: bounds.top, bottom: bounds.bottom,
        left: bounds.left, right: bounds.right };
    }).filter(number => viewport && number.top < viewport.bottom && number.bottom > viewport.top
      && number.left < viewport.right && number.right > viewport.left)
      .sort((left, right) => left.top - right.top);
    return {
      viewport: viewport?.toJSON() ?? null,
      firstVisibleLine: visibleNumbers[0]?.text ?? null,
      lastVisibleLine: visibleNumbers.at(-1)?.text ?? null,
      visibleLineCount: visibleNumbers.length,
      visibleLines: visibleNumbers.map(number => number.text),
    };
  }, index);
  const goTo = async (group, line, column) => {
    await command(group === 0 ? 'workbench.action.focusFirstEditorGroup' : 'workbench.action.focusSecondEditorGroup');
    await command('workbench.action.quickOpen', `:${line}:${column}`); await page.keyboard.press('Enter');
    await expect(page.locator('#status\\.editor\\.selection')).toContainText(new RegExp(`\\b${line}\\b.*\\b${column}\\b`));
  };
  await goTo(1, 120, 6); await goTo(0, 20, 5);
  const splitScroll = await Promise.all([0, 1].map(async group =>
    (await editorViewMetrics(group)).firstVisibleLine));
  expect(Number(splitScroll[1])).toBeGreaterThan(Number(splitScroll[0]));
  result.splitScroll = splitScroll;
  result.preReloadEditorViews = await Promise.all([editorViewMetrics(0), editorViewMetrics(1)]);
  await open(a);
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  const before = await views();
  for (const target of identities) expect(before[cacheKey(target)].timeline.orderedTurnIds.length).toBeGreaterThan(0);
  const drafts = Object.fromEntries(Object.entries(before).filter(([key]) => key.startsWith('draft:')));
  expect(Object.values(drafts).sort()).toEqual(['保留草稿 0', '保留草稿 1']);
  const tabCount = await page.locator('.tabs-container .tab').count();

  await checkpoint('cancel-command-from-f1');
  await command('workbench.action.showCommands');
  await quick.locator('input').first().fill('>清除当前 Thread 历史展示缓存');
  const action = quick.locator('.monaco-list-row').filter({ hasText: '清除当前 Thread 历史展示缓存' });
  await expect(action).toHaveCount(1); result.commandDiscoverable = true; await action.click();
  const dialog = page.locator('.monaco-dialog-box');
  await expect(dialog).toBeVisible();
  for (const id of [a.gateway_id, a.workspace_id, a.session_id, a.thread_id]) await expect(dialog).toContainText(id);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).toBeHidden();
  expect((await views())[cacheKey(a)]).toEqual(before[cacheKey(a)]); result.cancelPreserved = true;

  await checkpoint('clear-captured-owner-after-focus-change');
  await page.evaluate(() => {
    globalThis.cacheCleanup = { done: false };
    void globalThis.workbench.commands.executeCommand('boxteam.thread.clearHistoryCache')
      .then(() => { globalThis.cacheCleanup.done = true; }, error => { globalThis.cacheCleanup.error = String(error); });
  });
  await expect(dialog).toBeVisible();
  await command('workbench.action.nextEditor');
  await expect(pane(b).getByTestId('composer-input')).toHaveValue('保留草稿 1');
  await expect(dialog).toContainText(a.thread_id);
  await dialog.getByRole('button', { name: '清除展示缓存', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect.poll(() => page.evaluate(() => globalThis.cacheCleanup.done || globalThis.cacheCleanup.error)).toBe(true);
  const cleared = await views();
  expect(cleared[cacheKey(a)]).toBeUndefined(); result.capturedOwnerCleared = true;
  expect(cleared[cacheKey(b)].timeline.turnsById).toEqual(before[cacheKey(b)].timeline.turnsById);
  result.otherThreadPreserved = true;
  expect(Object.fromEntries(Object.entries(cleared).filter(([key]) => key.startsWith('draft:')))).toEqual(drafts);
  expect(await readSavedSettings(page)).toEqual(savedSettings);
  expect(await page.locator('.tabs-container .tab').count()).toBe(tabCount);
  expect((await recoveryRecords()).filter(record => record.key.includes('/History/'))).toEqual(protectedHistory);
  expect((await recoveryRecords()).some(record => record.key.includes('/Backups/') && record.text.endsWith(dirtyBody))).toBe(true);
  await command('workbench.action.quickOpen', '');
  for (const target of identities) await expect(quick.locator(`[data-quick-input-id="${resource(target)}"]`)).toHaveCount(1);
  await page.keyboard.press('Escape');

  await checkpoint('normal-reload-and-authoritative-rebuild');
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  result.savedBeforeReloadEditorViews = await Promise.all([editorViewMetrics(0), editorViewMetrics(1)]);
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  expect(await readSavedSettings(page)).toEqual(savedSettings); result.settingsRestored = true;
  for (const [index, target] of identities.entries()) {
    await open(target); await expect(pane(target).getByTestId('composer-input')).toHaveValue(`保留草稿 ${index}`);
  }
  result.draftsRestored = true;
  for (const [index, position] of [[0, [20, 5]], [1, [120, 6]]]) {
    await command(index === 0 ? 'workbench.action.focusFirstEditorGroup' : 'workbench.action.focusSecondEditorGroup');
    // 原内部命令的列从 0 开始，不是扩展 API 的 ViewColumn 枚举。
    await command('_workbench.open', fileUri, [index, { pinned: true }]);
    await expect(groups.nth(index).getByRole('tab', { name: /cache-protected\.txt/ })).toHaveClass(/dirty/);
    await expect(page.locator('#status\\.editor\\.selection')).toContainText(new RegExp(`\\b${position[0]}\\b.*\\b${position[1]}\\b`));
    const restoredView = await editorViewMetrics(index);
    result.restoredEditorViews = result.restoredEditorViews ?? [];
    result.restoredEditorViews.push({ group: index, view: restoredView });
    expect(restoredView.firstVisibleLine).toBe(splitScroll[index]);
  }
  expect((await recoveryRecords()).some(record => record.key.includes('/Backups/') && record.text.endsWith(dirtyBody))).toBe(true);
  result.dirtyBackupRestored = true; result.splitViewsRestored = true;
  expect((await recoveryRecords()).filter(record => record.key.includes('/History/'))).toEqual(protectedHistory);
  result.localHistoryPreserved = true;
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  const rebuilt = await views();
  expect(rebuilt[cacheKey(a)].timeline.turnsById).toEqual(before[cacheKey(a)].timeline.turnsById);
  result.historyRebuilt = true;
  expect(result.forbiddenWrites).toEqual([]); expect(result.pageErrors).toEqual([]);
  await checkpoint('complete');
} catch (cause) { failure = cause; }
finally {
  if (failure) {
    result.error = String(failure.stack ?? failure);
    try { if (page) await page.screenshot({ path: output.replace(/\.json$/, '.failure.png'), fullPage: true }); }
    catch (cause) { result.screenshotError = String(cause); }
  }
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  finally { await run.close(); }
}
if (failure) throw failure;
console.log(JSON.stringify({ phase: result.phase, output }));
