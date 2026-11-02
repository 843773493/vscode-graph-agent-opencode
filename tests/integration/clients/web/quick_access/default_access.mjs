import { expect } from 'playwright/test';
import { readSavedSettings } from './settings.mjs';
import { verifyResourceOpenActions, verifyResourcePresentation } from './default_interactions.mjs';

/** 名称状态通过产品创建入口生成；查询、打开及迟到结果均经过真实 Gateway。 */
export async function verifyDefaultAccess({ page, command, api, result, fileUri, ownerA, ownerB }) {
  result.phase = 'default-resource-quick-access';
  const requests = [];
  result.defaultQuickAccess = { requests };
  const observe = request => {
    const url = new URL(request.url());
    if (['/api/v1/session-catalog/search', '/api/v1/session-threads/search', '/api/v1/resources/navigation'].includes(url.pathname)) {
      const body = request.method() === 'POST' ? request.postDataJSON() : undefined;
      if (url.pathname === '/api/v1/resources/navigation' && body?.operation !== 'search') return;
      requests.push({ path: url.pathname, route: request.headers()['x-boxteam-workspace-id'], query: url.searchParams.get('query'),
        body });
    }
  };
  page.on('request', observe);
  const create = async owner => (await api('/api/v1/sessions', { method: 'POST', headers: { 'X-BoxTeam-Workspace-Id': owner.route },
    body: JSON.stringify({ title: `quickdiscover ${owner === ownerA ? 'A' : 'B'} 会话` }) })).data;
  const sessionA = await create(ownerA), sessionB = await create(ownerB);
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  let intercepted = false;
  const delay = async route => {
    const request = route.request();
    if (request.headers()['x-boxteam-workspace-id'] !== ownerA.route || new URL(request.url()).searchParams.get('query') !== 'quickdiscover') {
      await route.continue(); return;
    }
    const response = await route.fetch();
    intercepted = true;
    await blocked;
    await route.fulfill({ response });
  };
  try {
    await command('_workbench.open', await fileUri(ownerA), [undefined, { pinned: true }]);
    const beforeEmpty = requests.length;
    await page.keyboard.press('ControlOrMeta+P');
    await expect(quick).toBeVisible();
    await expect(input).toBeFocused();
    await expect(quick).toContainText('最近打开');
    await page.waitForTimeout(300);
    expect(requests.length).toBe(beforeEmpty);
    await page.route('**/api/v1/session-threads/search?**', delay);
    await input.fill('quickdiscover');
    await expect(quick).toContainText('quickdiscover A 会话');
    await expect.poll(() => intercepted).toBe(true);
    await expect(quick).toContainText('Thread · 正在查询');
    await expect(quick).not.toContainText('quickdiscover B 会话');
    // 真实 Thread 查询的响应被延迟；会话与文件分支不被它阻塞。
    release();
    await expect(quick).toContainText('Thread · 本次查询完成');
    await expect(quick).toContainText('文件 · 本次查询完成');
    await expect(quick).toContainText('quickdiscover-file.txt');
    await expect(quick).toContainText('工作区符号未启用');
    const first = requests.filter(request => request.query === 'quickdiscover' || request.body?.query === 'quickdiscover');
    expect(first.length).toBeGreaterThanOrEqual(3);
    expect(first.every(request => request.route === ownerA.route)).toBe(true);
    result.defaultListPresentation = await verifyResourcePresentation({ quick, sessionId: sessionA.session_id });
    const row = quick.locator('[data-quick-input-id^="session:"]')
      .filter({ hasText: `会话 · ${sessionA.session_id}` });
    await expect(row).toHaveCount(1);
    await row.click();
    await expect(quick).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /quickdiscover A 会话/ })).toBeVisible();
    await verifyResourceOpenActions({ page, command, api, result, owner: ownerA, session: sessionA });
    await command('workbench.action.quickOpen', 'quickdiscover-file.txt:2:3');
    const file = quick.getByRole('option', { name: /^quickdiscover-file\.txt / });
    await expect(file).toHaveCount(1);
    await expect(file).toHaveClass(/focused/);
    await page.keyboard.press('Enter');
    await expect(quick).not.toBeVisible();
    await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true }).locator('.view-lines')).toContainText('discover A line one');
    await expect(page.locator('[id="status.editor.selection"]')).toContainText('Ln 2, Col 3');
    await command('workbench.action.quickOpen', 'quickdiscover-dir/deep-file');
    const nested = quick.getByRole('option', { name: /^deep-file\.txt / });
    await expect(nested).toHaveCount(1);
    await expect(nested).toHaveClass(/focused/);
    await page.keyboard.press('Enter');
    await expect(page.locator('.editor-instance .monaco-editor').filter({ visible: true }).locator('.view-lines')).toContainText('nested discover A');
    const unopened = (await api('/api/v1/sessions', { method: 'POST', headers: { 'X-BoxTeam-Workspace-Id': ownerA.route },
      body: JSON.stringify({ title: 'quick-id-unopened A' }) })).data;
    const main = (await api(`/api/v1/sessions/${unopened.session_id}/main-thread`, { headers: { 'X-BoxTeam-Workspace-Id': ownerA.route } })).data;
    await command('workbench.action.quickOpen', main.thread_id);
    const thread = quick.getByRole('option', { name: new RegExp(`^quick-id-unopened A .*${main.thread_id}`) });
    await expect(thread).toHaveCount(1);
    await expect(thread).toHaveClass(/focused/);
    await page.keyboard.press('Enter');
    await expect(quick).not.toBeVisible();
    await expect(page.getByRole('tab', { name: /quick-id-unopened A/ })).toBeVisible();

    // 明确打开 B 文件后，新一次入口捕获 B；相同查询不得沿 A 的缓存或默认路由。
    await command('_workbench.open', await fileUri(ownerB), [undefined, { pinned: true }]);
    const beforeB = requests.length;
    await command('workbench.action.quickOpen', 'quickdiscover');
    await expect(quick).toContainText('quickdiscover B 会话');
    await expect(quick).toContainText('Thread · 本次查询完成');
    await expect(quick).toContainText('文件 · 本次查询完成');
    await expect(quick).not.toContainText('quickdiscover A 会话');
    const second = requests.slice(beforeB).filter(request => request.query === 'quickdiscover' || request.body?.query === 'quickdiscover');
    expect(second.length).toBeGreaterThanOrEqual(3);
    expect(second.every(request => request.route === ownerB.route)).toBe(true);
    await page.keyboard.press('Escape');
    const beforeMissing = requests.length;
    await command('workbench.action.quickOpen', 'quick-no-match-unique');
    await expect(quick).toContainText('会话 · 没有匹配的资源');
    await expect(quick).toContainText('Thread · 没有匹配的资源');
    await expect(quick).toContainText('文件 · 没有匹配的资源');
    expect(requests.slice(beforeMissing).every(request => request.route === ownerB.route)).toBe(true);
    await page.keyboard.press('Escape');
    const beforeSymbols = requests.length;
    await command('workbench.action.quickOpen', '#quickdiscover');
    await expect(input).toHaveAttribute('placeholder', `工作区符号 · ${ownerB.workspaceId}`);
    await expect(quick).toContainText('工作区符号未启用');
    expect(requests.length).toBe(beforeSymbols);
    await page.keyboard.press('Escape');
    result.workspaceSymbolsUnavailable = true;
    const beforeHelp = requests.length;
    await command('workbench.action.quickOpenWithModes');
    await expect(quick).toBeVisible();
    await expect(quick).toContainText(/Go to File|转到文件/);
    expect(requests.length).toBe(beforeHelp);
    await page.keyboard.press('Escape');

    // 原失焦设置保留浮层；底层焦点从 A 改成 B 后，帮助转接仍使用发起时的 A。
    await command('_workbench.open', await fileUri(ownerA), [undefined, { pinned: true }]);
    await command('workbench.action.quickOpenWithModes');
    await expect(quick).toBeVisible();
    await command('_workbench.open', await fileUri(ownerB), [undefined, { pinned: true }]);
    await expect(quick).toBeVisible();
    await input.fill('?');
    await quick.getByRole('option', { name: /^%,/ }).click();
    await expect(input).toHaveAttribute('placeholder', /搜索文件内容/);
    const beforeMode = requests.length;
    await input.fill('%quickmodeword');
    await expect(quick).toContainText('mode scope A quickmodeword');
    await expect(quick).not.toContainText('mode scope B quickmodeword');
    const modeRequests = requests.slice(beforeMode).filter(request => request.body?.query === 'quickmodeword');
    expect(modeRequests.length).toBeGreaterThan(0);
    expect(modeRequests.every(request => request.route === ownerA.route && request.body?.uri === ownerA.root)).toBe(true);
    await input.fill('?');
    await quick.getByRole('option', { name: /^…,/ }).click();
    await expect(quick).toContainText('最近打开');
    await expect(quick).toContainText(ownerA.workspaceId);
    await expect(quick).not.toContainText(ownerB.workspaceId);
    await expect(quick).not.toContainText(/Go to File|转到文件/);
    await page.keyboard.press('Escape');

    // 项目合同要求关闭历史同时隐藏空输入记录，不以枚举业务资源补空。
    await command('workbench.action.openWorkspaceSettings', '@id:search.quickOpen.includeHistory');
    const setting = page.locator('[data-key="search.quickOpen.includeHistory"]');
    const history = setting.getByRole('checkbox');
    await expect(history).toBeChecked();
    await history.click();
    await expect(history).not.toBeChecked();
    await page.locator('.settings-editor').getByRole('textbox', { name: /Search settings/ }).focus();
    // Modified 标记由原配置模型重绘，不能把点击后的乐观 checkbox 当作设置已发布。
    await expect(setting).toHaveClass(/is-configured/);
    await command('workbench.action.closeActiveEditor');
    await expect(setting).not.toBeVisible();
    const beforeDisabledHistory = requests.length;
    await command('workbench.action.quickOpen', '');
    await expect(quick).toBeVisible();
    await expect(quick.locator('[role="option"]')).toHaveCount(0);
    await page.waitForTimeout(300);
    expect(requests.length).toBe(beforeDisabledHistory);
    await page.keyboard.press('Escape');
    result.defaultQuickAccess = { sessionA: sessionA.session_id, sessionB: sessionB.session_id, requests, emptyHasNoQueries: true,
      progressive: true, scopeA: first, scopeB: second, helpHasNoQueries: true, disabledHistoryHasNoQueries: true,
      helpKeepsInvocation: true, fileSelection: '2:3', nestedPath: true, threadId: main.thread_id };
  } finally {
    release();
    await page.unroute('**/api/v1/session-threads/search?**', delay);
    page.off('request', observe);
    // 只读实际保存内容，为配置发布失败保留诊断，不替产品写入配置。
    result.savedSettings = await readSavedSettings(page);
    await command('workbench.action.closeAllEditors');
  }
}
