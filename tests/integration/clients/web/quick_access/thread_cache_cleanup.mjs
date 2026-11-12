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
  await command('workbench.action.quickOpen', '');
  for (const target of identities) await expect(quick.locator(`[data-quick-input-id="${resource(target)}"]`)).toHaveCount(1);
  await page.keyboard.press('Escape');

  await checkpoint('normal-reload-and-authoritative-rebuild');
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await page.reload({ waitUntil: 'domcontentloaded' }); await ready();
  expect(await readSavedSettings(page)).toEqual(savedSettings); result.settingsRestored = true;
  for (const [index, target] of identities.entries()) {
    await open(target); await expect(pane(target).getByTestId('composer-input')).toHaveValue(`保留草稿 ${index}`);
  }
  result.draftsRestored = true;
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
