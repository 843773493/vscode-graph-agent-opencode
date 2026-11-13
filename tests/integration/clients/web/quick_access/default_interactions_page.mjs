import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const base = process.env.BOXTEAM_BROWSER_BASE_URL;
const resultPath = process.env.BOXTEAM_BROWSER_RESULT_PATH;
const screenshotPath = process.env.BOXTEAM_BROWSER_SCREENSHOT_PATH;
if (!base || !resultPath || !screenshotPath) throw new Error('Quick Access 交互页缺少运行参数');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_quick_access_interactions.py');
const result = { classification: 'Integration', phase: 'initialization', pageErrors: [], consoleErrors: [], boundary: '正式静态 Workbench、真实 Gateway/Workspace/Chromium；不替换 Quick Input 或 Provider' };
let page, failure;
async function command(id, ...args) {
  await page.evaluate(async ({ id, args }) => { await globalThis.workbench.commands.executeCommand(id, ...args); }, { id, args });
}
async function api(path, init = {}) {
  return page.evaluate(async ({ path, init }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(path, { ...init, headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json', ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, status: response.status, body }));
    return body;
  }, { path, init });
}

try {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
  const context = await browser.newContext();
  run.addCleanup('BrowserContext', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push(message.text()); });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 60_000 });
  const active = (await api('/api/gateway/workspaces')).data.active_workspace;
  const headers = { 'X-BoxTeam-Workspace-Id': active.workspace_id, 'X-BoxTeam-Target-Gateway-Id': active.gateway_id };
  const workspace = await api('/api/v1/workspace', { headers });
  const roots = await api('/api/v1/workspace/files/roots', { headers });
  expect(workspace.data.workspace_id).toBe(active.workspace_id);
  result.initialization = { workspace: workspace.request_id, roots: roots.request_id, owner: active.workspace_id };
  const root = roots.data.workspace_uri;
  const uri = name => `boxteam-file://${active.gateway_id}/${active.workspace_id}/${encodeURIComponent(root)}/${name}`;
  await api('/api/v1/sessions', { method: 'POST', headers, body: JSON.stringify({ title: 'interaction-session-alpha' }) });
  await api('/api/v1/sessions', { method: 'POST', headers, body: JSON.stringify({ title: 'interaction-session-beta' }) });
  await command('_workbench.open', uri('qa-interaction-origin.txt'), [undefined, { pinned: true }]);
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const originalDarkTheme = await page.locator('.monaco-workbench').evaluate(element => element.classList.contains('vs-dark'));
  await command('workbench.action.toggleLightDarkThemes');
  await expect.poll(() => page.locator('.monaco-workbench').evaluate(element => element.classList.contains('vs-dark'))).toBe(!originalDarkTheme);
  await command('workbench.action.toggleLightDarkThemes');
  await expect.poll(() => page.locator('.monaco-workbench').evaluate(element => element.classList.contains('vs-dark'))).toBe(originalDarkTheme);
  result.phase = 'initial-focus-and-list';
  await command('workbench.action.quickOpen', 'qa-interaction-target');
  await expect(input).toBeFocused();
  await expect(input).not.toHaveAttribute('placeholder', '');
  await expect(input).toHaveAttribute('aria-controls');
  await expect(quick.getByRole('listbox')).toBeVisible();
  const fileRows = quick.locator('[data-quick-input-id^="boxteam-file:"]');
  const rowIsFocused = row => row.evaluate(element => element.closest('[role="option"]')?.classList.contains('focused') ?? false);
  const activeOption = () => input.evaluate(element => {
    const active = document.getElementById(element.getAttribute('aria-activedescendant') ?? '');
    const option = active?.closest('[role="option"]') ?? active?.querySelector('[role="option"]');
    return option && { id: option.id, position: option.getAttribute('aria-posinset'), size: option.getAttribute('aria-setsize'), label: option.getAttribute('aria-label') };
  });
  await expect.poll(async () => Number(await quick.getByRole('option').first().getAttribute('aria-setsize'))).toBeGreaterThanOrEqual(24);
  await expect(fileRows).not.toHaveCount(0);
  await expect.poll(() => rowIsFocused(fileRows.first())).toBe(true);
  await expect(quick.locator('.label-name .highlight').first()).toContainText('qa-interaction-target');
  await expect(fileRows.first().locator('.quick-input-list-icon')).toBeVisible();
  await fileRows.first().getByRole('button', { name: '在侧边打开' }).hover();
  await expect(page.getByRole('tooltip')).toContainText('在侧边打开');
  await expect(fileRows.first().locator('.label-description')).not.toBeEmpty();
  await expect(fileRows.first().locator('.quick-input-list-label-meta')).not.toBeEmpty();
  const aria = await input.evaluate(element => ({ controls: element.getAttribute('aria-controls'), active: element.getAttribute('aria-activedescendant') }));
  expect(aria.controls).toBeTruthy();
  expect(aria.active).toBeTruthy();
  result.initialFocusAndAccessibleList = true;

  result.phase = 'keyboard-boundaries';
  await input.press('ControlOrMeta+Home');
  await expect.poll(async () => (await activeOption())?.position).toBe('1');
  const firstOption = await activeOption();
  await input.press('ControlOrMeta+End');
  await expect.poll(async () => {
    const active = await activeOption();
    return active && active.position === active.size;
  }).toBe(true);
  const lastOption = await activeOption();
  expect(firstOption?.id).not.toBe(lastOption?.id);
  await input.press('ControlOrMeta+Home');
  const beforeArrowDown = Number((await activeOption())?.position);
  await input.press('ArrowDown');
  await expect.poll(async () => Number((await activeOption())?.position)).toBe(beforeArrowDown + 1);
  const beforeArrowUp = Number((await activeOption())?.position);
  await input.press('ArrowUp');
  await expect.poll(async () => Number((await activeOption())?.position)).toBe(beforeArrowUp - 1);
  await input.press('ControlOrMeta+End');
  const lastPosition = Number((await activeOption())?.position);
  await input.press('ArrowDown');
  await expect.poll(async () => Number((await activeOption())?.position)).toBe(1);
  await input.press('ControlOrMeta+Home');
  await input.press('ArrowUp');
  await expect.poll(async () => Number((await activeOption())?.position)).toBe(lastPosition);
  await input.press('ControlOrMeta+Home');
  const beforePageDown = Number((await activeOption())?.position);
  await input.press('PageDown');
  await expect.poll(async () => Number((await activeOption())?.position)).toBeGreaterThan(beforePageDown);
  const beforePageUp = Number((await activeOption())?.position);
  await input.press('PageUp');
  await expect.poll(async () => Number((await activeOption())?.position)).toBeLessThan(beforePageUp);
  await input.press('ArrowDown');
  await input.press('ArrowDown');
  await expect.poll(() => rowIsFocused(fileRows.first())).toBe(true);
  const activeBeforeRefresh = await input.evaluate(element => document.getElementById(element.getAttribute('aria-activedescendant') ?? '')?.querySelector('[data-quick-input-id]')?.getAttribute('data-quick-input-id'));
  const scrollBeforeRefresh = await quick.getByRole('listbox').evaluate(element => element.scrollTop);
  await input.fill('qa-interaction-target-');
  await expect.poll(async () => Number(await quick.getByRole('option').first().getAttribute('aria-setsize'))).toBeGreaterThanOrEqual(24);
  await expect.poll(() => input.evaluate(element => document.getElementById(element.getAttribute('aria-activedescendant') ?? '')?.querySelector('[data-quick-input-id]')?.getAttribute('data-quick-input-id'))).toBe(activeBeforeRefresh);
  const scrollAfterRefresh = await quick.getByRole('listbox').evaluate(element => element.scrollTop);
  expect(scrollAfterRefresh).toBeGreaterThanOrEqual(scrollBeforeRefresh);
  result.keyboardBoundaries = true;
  await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible();

  result.phase = 'modified-accept-and-background';
  await command('workbench.action.quickOpen', 'qa-interaction-target-03');
  await expect(quick.getByRole('option', { name: /^qa-interaction-target-03\.txt/ })).toHaveClass(/focused/);
  await input.press('ControlOrMeta+Enter');
  await expect(quick).not.toBeVisible();
  const pinnedTab = page.getByRole('tab', { name: /qa-interaction-target-03\.txt/ });
  await expect(pinnedTab).toBeVisible();
  await expect(pinnedTab.locator('.monaco-icon-label')).not.toHaveClass(/italic/);
  await command('workbench.action.quickOpen', 'qa-interaction-target-04');
  await expect(input).toHaveValue('qa-interaction-target-04');
  await expect(quick.getByRole('option', { name: /^qa-interaction-target-04\.txt/ })).toHaveClass(/focused/);
  await input.press('End');
  await input.press('ArrowRight');
  await expect(quick).toBeVisible();
  await expect(input).toBeFocused();
  await expect(page.getByRole('tab', { name: /qa-interaction-target-04\.txt/ })).toBeVisible();
  await page.keyboard.press('Escape');
  await command('workbench.action.quickOpen', 'qa-interaction-target-05');
  await expect(quick.getByRole('option', { name: /^qa-interaction-target-05\.txt/ })).toHaveClass(/focused/);
  await input.press('Alt+Enter');
  await expect(quick).not.toBeVisible();
  const target05Tab = page.getByRole('tab', { name: /qa-interaction-target-05\.txt/ });
  const originGroup = page.locator('.editor-group-container').filter({ has: page.getByRole('tab', { name: /qa-interaction-origin\.txt/ }) });
  await expect(page.locator('.editor-group-container').filter({ has: target05Tab })).toHaveCount(1);
  await expect(originGroup.getByRole('tab', { name: /qa-interaction-target-05\.txt/ })).toHaveCount(0);
  result.modifiedAcceptAndBackground = true;

  result.phase = 'help-back-and-escape';
  await command('workbench.action.quickOpenWithModes');
  await expect(quick).toBeVisible();
  await input.fill('?');
  const help = quick.getByRole('option', { name: /^%,/ });
  await expect(help).toBeVisible();
  await help.click();
  await expect(input).toHaveAttribute('placeholder', /搜索文件内容/);
  await input.fill('?');
  const back = quick.getByRole('option', { name: /^…,/ });
  await expect(back).toBeVisible();
  await back.click();
  await expect(input).toHaveAttribute('placeholder', /查找会话/);
  await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible();
  result.escapeBackAndFocusLost = true;

  result.phase = 'preview-cancel-restores-position';
  const editor = page.locator('.editor-group-container.active .editor-instance .monaco-editor').filter({ visible: true });
  await command('_workbench.open', uri('qa-interaction-origin.txt'), [undefined, { pinned: true }]);
  await editor.locator('.view-lines').click();
  await page.keyboard.press('ControlOrMeta+End');
  const selection = page.locator('[id="status.editor.selection"]');
  const before = await selection.innerText();
  await command('workbench.action.quickOpen', 'qa-interaction-symbols');
  await expect(quick.getByRole('option', { name: /^qa-interaction-symbols\.json/ })).toBeVisible();
  await input.fill('qa-interaction-symbols@beta');
  const beta = quick.getByRole('option').filter({ hasText: 'beta' });
  await expect(beta).toHaveClass(/focused/);
  await expect(editor.locator('.rangeHighlight')).not.toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(selection).toHaveText(before);
  await expect(editor.locator('.rangeHighlight')).toHaveCount(0);
  result.previewCancelRestoresPosition = true;

  result.phase = 'focus-lost-and-settings';
  await writeWorkspaceSettings({ page, command }, { 'workbench.quickOpen.closeOnFocusLost': false,
    'workbench.quickOpen.preserveInput': true, 'workbench.editor.enablePreviewFromQuickOpen': true,
    'editor.minimap.enabled': false });
  await command('workbench.action.quickOpen', 'qa-interaction');
  await input.fill('qa-interaction-remember');
  await command('_workbench.open', uri('qa-interaction-origin.txt'), [undefined, { pinned: true }]);
  await expect(quick).toBeVisible();
  await input.fill('qa-interaction-target-01');
  await expect(quick.getByRole('option').first()).toHaveClass(/focused/);
  await page.keyboard.press('Escape');
  await expect(quick).not.toBeVisible();
  await command('workbench.action.quickOpen');
  await expect(input).toHaveValue('qa-interaction-target-05');
  await page.keyboard.press('Escape');
  await command('workbench.action.quickOpen', 'qa-interaction-target-01');
  await expect(quick.getByRole('option', { name: /^qa-interaction-target-01\.txt/ })).toHaveClass(/focused/);
  await input.press('Enter');
  await expect(quick).not.toBeVisible();
  await command('workbench.action.quickOpen');
  await expect(input).toHaveValue('qa-interaction-target-01');
  await page.keyboard.press('Escape');
  await writeWorkspaceSettings({ page, command }, { 'workbench.quickOpen.closeOnFocusLost': true,
    'workbench.quickOpen.preserveInput': false, 'workbench.editor.enablePreviewFromQuickOpen': true,
    'editor.minimap.enabled': false });
  await command('workbench.action.quickOpen', 'qa-interaction-target-02');
  await command('_workbench.open', uri('qa-interaction-origin.txt'), [undefined, { pinned: true }]);
  await expect(quick).not.toBeVisible();
  result.escapeBackAndFocusLost = true;
  await command('workbench.action.quickOpen');
  await expect(input).toHaveValue('');
  await page.keyboard.press('Escape');
  await command('workbench.action.showCommands');
  await expect(quick).toBeVisible();
  await expect(quick.getByRole('option')).not.toHaveCount(0);
  await expect(quick.getByRole('option').first()).toHaveClass(/focused/);
  await page.keyboard.press('Escape');
  result.layeredResultsAndCommands = true;
  result.inputMemoryAndActiveScroll = true;

  result.phase = 'composition-theme';
  await command('workbench.action.quickOpen', 'qa-interaction-target');
  const cdp = await context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: '中文', selectionStart: 2, selectionEnd: 2 });
  await expect(input).toHaveValue(/中文/);
  const theme = await page.locator('.monaco-workbench').getAttribute('class');
  await page.keyboard.press('Escape');
  result.compositionAndTheme = { compositionEvents: true, themeClass: theme, aria };
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error?.stack ?? error);
} finally {
  if (page && failure) {
    try { await page.screenshot({ path: screenshotPath, fullPage: true }); } catch (error) { result.screenshotError = String(error); }
  }
  result.phase = failure ? 'failed' : result.phase;
  try {
    await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n');
  } catch (error) {
    failure ??= error;
    result.writeError = String(error?.stack ?? error);
  } finally {
    await run.close();
  }
}
if (failure) throw failure;
