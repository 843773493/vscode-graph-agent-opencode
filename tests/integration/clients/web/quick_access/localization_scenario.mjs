import { writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { expect } from 'playwright/test';
import { writeUserSettings, writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';
import { testControl } from '../../../../support/e2e_control/browser_client.mjs';

const required = name => { const value = process.env[name]; if (!value) throw new Error(`显示语言验收缺少 ${name}`); return value; };
export async function runLocalization({ run, launch }) {
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], consoleErrors: [], modelRequests: [],
  externalTranslationRequests: [], localeRequests: [], receipts: [], reloads: [], timings: [],
  boundary: '真实 Gateway／Workspace 与正式 Workbench。语言只经原 Quick Pick／清除命令更改；Thread、草稿、脏文件和分屏从产品生成，不预写语言、缓存或恢复状态。' };
let page, failure, executionCallId;
try {
  const host = await launch({ run, result, usePage(next) {
    page = next;
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors.push({ phase: result.phase, text: message.text() }); });
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) result.reloads.push({ phase: result.phase, url: frame.url() }); });
  page.on('request', request => {
    const url = new URL(request.url());
    if (/locali[sz]ation|locale|nls|l10n|i18n/i.test(url.href)) result.localeRequests.push({ phase: result.phase, url: url.href });
    if (/^https?:$/.test(url.protocol) && url.origin !== new URL(page.url()).origin && /locale|nls|l10n|i18n|gallery|marketplace|vsix/i.test(url.href)) {
      result.externalTranslationRequests.push(url.href);
    }
    if (request.method() === 'POST' && /\/threads\/[^/]+\/messages$/.test(url.pathname)) result.modelRequests.push(url.pathname);
  });
  } });
  const base = host.baseUrl;
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const api = async (path, owner, body) => {
    const receipt = await page.evaluate(async ({ path, owner, body }) => {
      const credential = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
        headers: { 'X-Local-Token': credential, 'Content-Type': 'application/json', ...(owner ? {
          'X-BoxTeam-Target-Gateway-Id': owner.gatewayId, 'X-BoxTeam-Workspace-Id': owner.workspaceId,
        } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
      return { status: response.status, payload: await response.json(), requestId: response.headers.get('X-Request-ID') };
    }, { path, owner, body });
    expect(receipt.status).toBe(200); expect(receipt.payload.request_id).toBe(receipt.requestId);
    result.receipts.push({ phase: result.phase, path, requestId: receipt.requestId });
    return receipt.payload.data;
  };
  const ready = async locale => {
    await expect.poll(() => page.evaluate(() => typeof globalThis.workbench?.commands?.executeCommand), { timeout: 30_000 }).toBe('function');
    await command('workbench.view.explorer');
    await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('html')).toHaveAttribute('lang', locale);
  };
  const verifyLateMarkdownPreview = async (locale, expectedWarning, expectedLabel, resourceLocale, hasBundle) => {
    result.phase = `late-markdown-preview-${locale}`;
    const uri = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/locale-preview.md`;
    await open(uri);
    await command('markdown.showPreview');
    const preview = page.frameLocator('iframe.webview:visible').frameLocator('iframe#active-frame');
    await expect(preview.locator('h1')).toHaveText('Markdown locale preview');
    const warning = preview.locator('#code-csp-warning');
    await expect(warning).toBeVisible({ timeout: 30_000 });
    await expect(warning).toHaveText(expectedWarning);
    await expect(warning).toHaveAttribute('aria-label', expectedLabel);
    const allMarkdownBundles = result.localeRequests.filter(item => item.url.includes('/localization/extensions/vscode.markdown-language-features-'));
    const markdownBundle = allMarkdownBundles.find(item => item.phase === resourceLocale);
    expect(Boolean(markdownBundle), `${resourceLocale} 的 Markdown 扩展 bundle 身份不匹配`).toBe(hasBundle);
    if (markdownBundle) expect(new URL(markdownBundle.url).origin).toBe(new URL(base).origin);
    result.markdownPreviews ??= [];
    result.markdownPreviews.push({ locale, warning: await warning.textContent(), markdownBundle: markdownBundle?.url ?? null });
    await command('workbench.action.closeActiveEditor');
    const sourceTab = groups.last().getByRole('tab', { name: /locale-preview\.md/ });
    await expect(sourceTab).toBeVisible();
    await sourceTab.click();
    await command('workbench.action.closeActiveEditor');
    await expect(page.locator('iframe.webview:visible')).toHaveCount(0);
  };
  const verifyImagePreview = async locale => {
    result.phase = `image-preview-${locale}`;
    await open(`boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/assets/test.jpg`);
    const viewer = page.locator('.image-preview-editor:visible .image-viewer');
    await expect(viewer).toHaveClass(/ready/);
    const label = locale === 'en' ? 'Image Preview: test.jpg' : '图片预览：test.jpg';
    await expect(viewer).toHaveAttribute('aria-label', label);
    expect(await viewer.locator('img').evaluate(image => image.naturalWidth)).toBeGreaterThan(0);
    await page.evaluate(() => { void globalThis.workbench.commands.executeCommand('imagePreview.selectZoomLevel'); });
    await expect(quick.locator('input').first()).toHaveAttribute('placeholder', locale === 'en' ? 'Select zoom level' : '选择缩放级别');
    const fit = locale === 'en' ? 'Whole Image' : '完整图片';
    await quick.locator('.monaco-list-row').filter({ hasText: fit }).click();
    await expect(quick).not.toBeVisible();
    result.imagePreviews ??= [];
    result.imagePreviews.push({ locale, label, fit });
    await command('workbench.action.closeActiveEditor');
  };
  const verifyPluginLanguage = async locale => {
    await command('workbench.view.extensions');
    const view = page.locator('.service-plugin-packages-view');
    await expect(view).toBeVisible();
    await expect(view.getByRole('searchbox')).toHaveAttribute('aria-label', locale === 'en' ? 'Search installed plugin packages' : '搜索已安装插件包');
    const scope = view.getByRole('combobox', { name: locale === 'en' ? 'Plugin management scope' : '插件管理范围', exact: true });
    await expect(scope).toHaveValue('gateway');
    await expect(scope.locator('option')).toHaveText(locale === 'en'
      ? ['Current Gateway', 'Current Workspace', 'Future Workspaces'] : ['当前 Gateway', '当前工作区', '以后新建工作区']);
    await expect(view.getByRole('button', { name: locale === 'en' ? 'Install Plugin Package' : '安装插件包', exact: true })).toBeVisible();
    await command('workbench.view.explorer');
  };
  let started = performance.now();
  await page.goto(base, { waitUntil: 'domcontentloaded' }); await ready('zh-CN');
  result.timings.push({ operation: 'cold-start', elapsedMs: performance.now() - started });
  await verifyPluginLanguage('zh-cn');
  expect(await page.evaluate(() => navigator.language)).toBe('en-US');
  expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
  result.defaultChinese = true;
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  const owner = { gatewayId: health.gateway_id, workspaceId: listing.active_workspace.workspace_id };
  expect((await api('/api/v1/workspace', owner)).workspace_id).toBe(owner.workspaceId);
  const identities = [];
  for (const title of ['语言 A', '语言 B']) {
    const created = await api('/api/v1/sessions', owner, { title });
    const thread = await api(`/api/v1/sessions/${created.session_id}/main-thread`, owner);
    identities.push({ ...owner, sessionId: created.session_id, threadId: thread.thread_id });
  }
  result.identities = identities;
  let quick = page.locator('.quick-input-widget');
  const reloadDialog = page.locator('.monaco-dialog-box[role="dialog"]');
  const open = async uri => {
    await command('workbench.action.quickOpen', uri);
    await quick.locator(`[data-quick-input-id="link:${uri}"]`).click();
    await expect(quick).not.toBeVisible();
  };
  const pane = identity => page.locator(`section.boxteam-thread[data-workspace-id="${identity.workspaceId}"]`
    + `[data-session-id="${identity.sessionId}"][data-thread-id="${identity.threadId}"]`);
  const threadUri = identity => `boxteam://${identity.gatewayId}/workspace/${identity.workspaceId}/resources/session/${identity.sessionId}/threads/${identity.threadId}`;
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off' });
  for (const [index, identity] of identities.entries()) {
    await open(threadUri(identity));
    await pane(identity).getByTestId('composer-input').fill(`保留草稿 ${index} {0} /home/alice/file.txt`);
    await expect(pane(identity).getByTestId('composer-send')).toHaveAttribute('aria-label', '发送消息');
  }
  result.phase = 'product-dirty-file-and-split';
  const file = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/locale.txt`;
  await open(file);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  const editor = group => group.locator('.editor-instance .monaco-editor').filter({ visible: true });
  let groups = page.locator('.editor-group-container');
  const dirty = '未保存的正文 {0} /home/alice/file.txt\n' + Array.from({ length: 99 }, (_, index) => `locale line ${index + 1}`).join('\n');
  const input = editor(groups.first()).getByRole('textbox');
  await input.focus(); await input.press('ControlOrMeta+A');
  await page.evaluate(text => navigator.clipboard.writeText(text), dirty); await input.press('ControlOrMeta+V');
  await expect(groups.first().getByRole('tab', { name: /locale\.txt/ })).toHaveClass(/dirty/);
  // Monaco只渲染可见行；粘贴后光标在末行，先沿真实编辑器操作回到首行再核原文。
  await input.press('ControlOrMeta+Home');
  await command('workbench.action.splitEditorRight');
  await expect(groups).toHaveCount(2);
  await expect(editor(groups.last()).locator('.view-lines')).toContainText('未保存的正文 {0}');

  const showLanguagePicker = async () => {
    const started = performance.now();
    await command('workbench.action.configureLocale');
    await expect(quick).toBeVisible();
    await expect(quick).toContainText('English');
    result.timings.push({ operation: 'language-picker', elapsedMs: performance.now() - started, requestId: null });
  };
  const chooseLanguage = async label => {
    await showLanguagePicker();
    await quick.locator('.monaco-list-row').filter({ hasText: label }).click();
    await expect(reloadDialog).toBeVisible();
  };
  result.phase = 'cancel-language-reload';
  await page.evaluate(() => {
    // 只拒绝这一次具名语言偏好写入，其它Storage及产品恢复均沿真实实现。
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      if (this === localStorage && key === 'vscode.nls.locale') {
        Storage.prototype.setItem = original;
        throw new Error('language preference denied /home/alice {0}');
      }
      return original.call(this, key, value);
    };
  });
  const reloadCount = result.reloads.length;
  await showLanguagePicker();
  await quick.locator('.monaco-list-row').filter({ hasText: 'English' }).click();
  await expect(page.locator('.notifications-toasts')).toContainText('language preference denied /home/alice {0}');
  expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await expect(reloadDialog).not.toBeVisible();
  expect(result.reloads.length).toBe(reloadCount);
  result.writeFailureVisible = true;
  await showLanguagePicker();
  await expect(quick).toContainText('English'); await expect(quick).toContainText('简体中文');
  await page.keyboard.press('Escape');
  expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
  await chooseLanguage('English');
  await reloadDialog.getByRole('button', { name: /取消|Cancel/ }).click();
  await ready('zh-CN');
  expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBe('en');
  await expect(editor(groups.last()).locator('.view-lines')).toContainText('未保存的正文 {0}');
  result.cancelPreserved = true;

  result.phase = 'start-background-execution';
  const runningSession = await api('/api/v1/sessions', owner, { title: '语言重载中的执行' });
  const runningThread = await api(`/api/v1/sessions/${runningSession.session_id}/main-thread`, owner);
  const runningIdentity = { ...owner, sessionId: runningSession.session_id, threadId: runningThread.thread_id };
  const planned = await testControl({ action: 'plan_model', session_id: runningIdentity.sessionId, thread_id: runningIdentity.threadId,
    interactions: JSON.parse(required('BOXTEAM_LOCALIZATION_MODEL_INTERACTIONS')) });
  expect(planned).toHaveLength(1);
  executionCallId = planned[0];
  await testControl({ action: 'arm', operation_id: executionCallId, phase: 'model.response_ready' });
  await open(threadUri(runningIdentity));
  await pane(runningIdentity).getByTestId('composer-input').fill('语言切换时继续执行，不要重放');
  const acceptance = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/v1/sessions/${runningIdentity.sessionId}/threads/${runningIdentity.threadId}/messages`);
  await pane(runningIdentity).getByTestId('composer-send').click();
  const acceptedResponse = await acceptance;
  expect(acceptedResponse.status()).toBe(200);
  const acceptedPayload = await acceptedResponse.json();
  expect(acceptedPayload.request_id).toBe(acceptedResponse.headers()['x-request-id']);
  const accepted = acceptedPayload.data;
  expect(accepted.thread_id).toBe(runningIdentity.threadId);
  await testControl({ action: 'acknowledged', operation_id: executionCallId, phase: 'model.response_ready' });
  const runningBefore = await api(`/api/v1/jobs/${accepted.job_id}`, owner);
  expect(runningBefore.status).toBe('running');
  result.backgroundExecution = { ...runningIdentity, ...accepted, modelCallId: executionCallId, acceptanceRequestId: acceptedPayload.request_id };

  result.phase = 'english-via-original-reload';
  await chooseLanguage('English');
  // 只拒绝真实展示存储的下一次写入；语言选择及宿主重载仍沿原链路。
  await page.evaluate(() => {
    const original = IDBObjectStore.prototype.put;
    globalThis.localizationCommitFailure = { writes: [] };
    IDBObjectStore.prototype.put = function(value, key) {
      if (this.transaction.db.name.startsWith('vscode-web-state-db-')) {
        IDBObjectStore.prototype.put = original;
        globalThis.localizationCommitFailure.writes.push({ database: this.transaction.db.name, store: this.name, key });
        this.transaction.abort();
        throw new Error('language reload view commit denied /home/alice {0}');
      }
      return original.call(this, value, key);
    };
  });
  const beforeCommitFailure = result.reloads.length;
  await reloadDialog.getByRole('button', { name: /重新加载|重载|Reload/ }).click();
  await expect(page.locator('.notifications-toasts')).toContainText('language reload view commit denied /home/alice {0}');
  expect(result.reloads.length).toBe(beforeCommitFailure);
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  result.commitFailure = await page.evaluate(() => globalThis.localizationCommitFailure);
  expect(result.commitFailure.writes).toHaveLength(1);
  await expect(pane(runningIdentity).getByTestId('composer-input')).toBeVisible();

  result.phase = 'cancel-dirty-reload';
  await writeUserSettings({ page, command }, { 'files.hotExit': 'off' });
  await open(file);
  await chooseLanguage('English');
  const beforeDirtyCancellation = result.reloads.length;
  // Electron 由原生宿主处理 beforeunload；Web 沿真实 dialog dismiss。
  // TODO：原生 run-2d0ecc047c9a 在第二组 dirty 断言失败，尚未核验下方新增的主进程事件观察；补验时保留双组断言。
  if (host.willPreventUnloadEvents) {
    const before = await host.willPreventUnloadEvents();
    await reloadDialog.getByRole('button', { name: /重新加载|重载|Reload/ }).click();
    await expect.poll(host.willPreventUnloadEvents).toHaveLength(before.length + 1);
    result.dirtyReloadDialog = { willPreventUnload: await host.willPreventUnloadEvents() };
    expect(result.dirtyReloadDialog.willPreventUnload.at(-1).defaultPrevented).toBe(false);
    expect(result.reloads.length).toBe(beforeDirtyCancellation);
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    for (const group of await groups.all()) await expect(group.getByRole('tab', { name: /locale\.txt/ })).toHaveClass(/dirty/);
  } else {
    const dialogShown = page.waitForEvent('dialog');
    const clicking = reloadDialog.getByRole('button', { name: /重新加载|重载|Reload/ }).click();
    const dialog = await dialogShown;
    expect(dialog.type()).toBe('beforeunload');
    result.dirtyReloadDialog = { type: dialog.type(), message: dialog.message() };
    await dialog.dismiss();
    await clicking;
    expect(result.reloads.length).toBe(beforeDirtyCancellation);
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    for (const group of await groups.all()) await expect(group.getByRole('tab', { name: /locale\.txt/ })).toHaveClass(/dirty/);
  }
  await writeUserSettings({ page, command }, { 'files.hotExit': 'onExitAndWindowClose' });
  result.dirtyCancellationPreserved = true;

  await chooseLanguage('English');
  started = performance.now();
  result.phase = 'english-reload';
  await Promise.all([page.waitForEvent('domcontentloaded'),
    reloadDialog.getByRole('button', { name: /重新加载|重载|Reload/ }).click()]);
  await ready('en');
  result.timings.push({ operation: 'reload-en', elapsedMs: performance.now() - started });
  const runningAfter = await api(`/api/v1/jobs/${accepted.job_id}`, owner);
  expect(runningAfter.status).toBe('running');
  expect(runningAfter.job_id).toBe(runningBefore.job_id);
  result.backgroundExecution.survivedEnglishReload = true;
  await verifyPluginLanguage('en');
  await verifyLateMarkdownPreview('en', 'Some content has been disabled in this document', 'Content Disabled Security Warning', 'english-reload', false);
  await verifyImagePreview('en');
  result.englishReloaded = true;
  await showLanguagePicker();
  await expect(quick.locator('input').first()).toHaveAttribute('placeholder', 'Select Display Language');
  await expect(quick).toContainText('English'); await page.keyboard.press('Escape');
  const verifyRecovered = async sendLabel => {
    await expect(groups).toHaveCount(2);
    for (const group of await groups.all()) {
      const tab = group.getByRole('tab', { name: /locale\.txt/ });
      await expect(tab).toHaveClass(/dirty/);
      await tab.click();
      await expect(editor(group).locator('.view-lines')).toContainText('未保存的正文 {0}');
    }
    for (const [index, identity] of identities.entries()) {
      await open(threadUri(identity));
      await expect(pane(identity).getByTestId('composer-input')).toHaveValue(`保留草稿 ${index} {0} /home/alice/file.txt`);
      await expect(pane(identity).getByTestId('composer-send')).toHaveAttribute('aria-label', sendLabel);
    }
  };
  await verifyRecovered('Send message');
  result.draftsRestored = true; result.dirtyRestored = true; result.splitRestored = true;
  result.phase = 'clear-language-preference';
  // 恢复两组文件为当前输入，清除偏好也走同一宿主重载。
  await command('workbench.action.focusSecondEditorGroup');
  await open(file);
  await page.evaluate(() => {
    void globalThis.workbench.commands.executeCommand('workbench.action.clearLocalePreference');
  });
  await expect(reloadDialog).toBeVisible();
  started = performance.now();
  result.phase = 'chinese-reload';
  await Promise.all([page.waitForEvent('domcontentloaded'),
    reloadDialog.getByRole('button', { name: /Reload/ }).click()]);
  await ready('zh-CN');
  result.timings.push({ operation: 'reload-zh-cn', elapsedMs: performance.now() - started });
  await verifyPluginLanguage('zh-cn');
  await verifyLateMarkdownPreview('zh-CN', '已禁用此文档中的部分内容', '已禁用内容安全警告', 'late-markdown-preview-zh-CN', true);
  await verifyImagePreview('zh-cn');
  expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
  await verifyRecovered('发送消息'); result.clearedToChinese = true;
  const after = await api('/api/gateway/workspaces');
  expect(after.active_workspace).toEqual(listing.active_workspace);
  for (const identity of identities) expect((await api(`/api/v1/sessions/${identity.sessionId}/main-thread`, owner)).thread_id).toBe(identity.threadId);
  result.backendIdentitiesPreserved = true;
  expect((await api(`/api/v1/jobs/${accepted.job_id}`, owner)).status).toBe('running');
  result.backgroundExecution.survivedChineseReload = true;
  await testControl({ action: 'release', operation_id: executionCallId, phase: 'model.response_ready' });
  await expect.poll(async () => {
    const job = await api(`/api/v1/jobs/${accepted.job_id}`, owner);
    if (['failed', 'cancelled', 'timed_out', 'execution_lost'].includes(job.status)) throw new Error(JSON.stringify(job));
    return job.status;
  }, { timeout: 30_000 }).toBe('completed');
  await open(threadUri(runningIdentity));
  await expect(pane(runningIdentity).locator(`.chat-turn[data-turn-id="${accepted.turn_id}"]`)
    .getByText('语言切换后执行完成 /home/alice/{0}', { exact: true })).toBeVisible();
  expect(result.modelRequests).toHaveLength(1);
  result.backgroundExecution.completedOnce = true;
  expect(result.pageErrors).toEqual([]); expect(result.externalTranslationRequests).toEqual([]);
  if (host.leave && host.restore) {
    result.phase = 'native-close-and-restart';
    // 语言重载必须留在原 renderer 所属窗口，不能重启 main 或后台服务。
    expect(result.native.launches).toHaveLength(1);
    await host.leave();
    await host.restore();
    quick = page.locator('.quick-input-widget');
    groups = page.locator('.editor-group-container');
    await ready('zh-CN');
    expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
    await verifyRecovered('发送消息');
    result.nativeRestartRestored = true;
  }
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  result.phase = 'complete';
} catch (error) {
  failure = error; result.error = error instanceof Error ? error.stack : String(error);
  if (page) result.frames = page.frames().map(frame => ({ name: frame.name(), url: frame.url() }));
  if (page) await page.screenshot({ path: join(dirname(output), 'failure.png') }).catch(cause => { result.screenshotError = String(cause); });
} finally {
  try {
    if (executionCallId) await testControl({ action: 'release', operation_id: executionCallId, phase: 'model.response_ready' });
  } finally {
    try { await run.close(); }
    finally { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); }
  }
}
if (failure) throw failure;
return result;
}
