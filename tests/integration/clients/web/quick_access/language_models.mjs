// Integration：模型回放仅供后端启动，JSON 分析使用原 Web 扩展及真实 Worker。
import { dirname, join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const required = name => {
  const value = process.env[name];
  if (!value) throw new Error(`语言模型验收缺少 ${name}`);
  return value;
};
const output = required('BOXTEAM_BROWSER_RESULT_PATH');
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_language_models.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], workerScripts: [], reads: [],
  boundary: '真实Gateway／后端／正式Workbench／原JSON扩展和Worker；无响应替身，未预写模型或分析结果，模型回放仅供后端启动' };
let page, failure;
try {
  const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('language-browser', () => browser.close());
  const context = await browser.newContext({ viewport: { width: 1800, height: 1050 } });
  run.addCleanup('language-context', () => context.close());
  page = await context.newPage();
  page.on('pageerror', error => result.pageErrors.push(String(error)));
  context.on('response', response => {
    if (/\/(extensionHostWorkerMain|jsonServerMain)\.js(?:\?|$)/.test(response.url())) {
      result.workerScripts.push({ url: response.url(), status: response.status() });
    }
  });
  context.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/workspace/files/raw') && url.searchParams.get('path')?.endsWith('/same.json')) {
      result.reads.push({ phase: result.phase, uri: url.searchParams.get('path'),
        workspaceId: request.headers()['x-boxteam-workspace-id'], gatewayId: request.headers()['x-boxteam-target-gateway-id'] });
    }
  });
  await page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 });
  const api = (path, init = {}) => page.evaluate(async ({ path, init }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, body }));
    }
    return body.data;
  }, { path, init });
  const headers = target => ({ 'X-BoxTeam-Target-Gateway-Id': target.gateway_id, 'X-BoxTeam-Workspace-Id': target.workspace_id });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  const targetA = listing.active_workspace;
  expect(targetA.gateway_id).toBe(health.gateway_id);
  const registered = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({
    root_path: required('BOXTEAM_LANGUAGE_OTHER_WORKSPACE'), name: 'Language B',
  }) });
  const targetB = registered.items.find(item => item.name === 'Language B');
  expect(targetB).toBeTruthy();
  const targets = [];
  for (const target of [targetA, targetB]) {
    const workspace = await api('/api/v1/workspace', { headers: headers(target) });
    const roots = await api('/api/v1/workspace/files/roots', { headers: headers(target) });
    expect(workspace.workspace_id).toBe(target.workspace_id);
    targets.push({ gatewayId: target.gateway_id, workspaceId: target.workspace_id, rootUri: roots.workspace_uri });
  }
  result.targets = targets;
  expect(targets[0].workspaceId).not.toBe(targets[1].workspaceId);
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const uri = target => page.evaluate(target => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: target.gatewayId,
    path: `/${target.workspaceId}/${encodeURIComponent(target.rootUri)}/same.json` }).toString(), target);
  const resources = await Promise.all(targets.map(uri));
  const symbols = resource => page.evaluate(async resource => {
    // vscode.* 是扩展宿主别名；工作台命令服务使用它映射的原注册入口。
    const symbols = await globalThis.workbench.commands.executeCommand('_executeDocumentSymbolProvider', globalThis.workbench.URI.parse(resource));
    return symbols?.map(symbol => symbol.name) ?? [];
  }, resource);
  const groups = page.locator('.editor-group-container');
  const editor = index => groups.nth(index).locator('.editor-instance .monaco-editor').filter({ visible: true });
  // 同一前景色可能附带括号匹配等装饰；只比较原 token metadata 的数字颜色类。
  const renderedTokenClasses = index => editor(index).locator('.view-line').first().locator('span[class^="mtk"]')
    .evaluateAll(spans => [...new Set(spans.flatMap(span => [...span.classList].filter(name => /^mtk\d+$/.test(name))))]);
  const inspectJsonToken = async (index, name) => {
    await editor(index).getByRole('textbox').focus();
    await page.keyboard.press('ControlOrMeta+Home');
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('ArrowRight');
    await command('editor.action.inspectTMScopes');
    const widget = editor(index).locator('.token-inspect-widget');
    await expect(widget).toBeVisible();
    await expect(widget.locator('.tiw-token')).toContainText(name);
    await expect(widget.locator('tr').filter({ has: page.getByText('language', { exact: true }) })
      .locator('.tiw-metadata-value')).toHaveText('json');
    await expect(widget.locator('.tiw-metadata-scopes')).toContainText('support.type.property-name.json');
    const text = await widget.textContent();
    expect(text).toContain(name);
    await command('editor.action.inspectTMScopes');
    await expect(widget).toHaveCount(0);
    return text;
  };
  const changeLanguage = async (id, label) => {
    await editor(1).getByRole('textbox').focus();
    // TODO：固定上游的字符串参数只赋 label、未赋 id；本项沿真实选择器验证语言切换。
    await Promise.all([command('workbench.action.editor.changeLanguageMode'), (async () => {
      const quick = page.locator('.quick-input-widget');
      await expect(quick).toBeVisible();
      await quick.locator('input').fill(label);
      await quick.locator(`[data-quick-input-id="${id}"]`).click();
    })()]);
    // CodeEditorWidget 把模型语言登记在宿主容器，内部 .monaco-editor 是渲染节点。
    await expect.poll(() => editor(1).evaluate(node => node.closest('[data-mode-id]')?.getAttribute('data-mode-id'))).toBe(id);
  };
  await writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off' });
  // 先通过原命令关闭初始 README，之后才能把关闭 A 视为释放该组最后一个编辑器。
  await command('workbench.action.closeAllEditors');
  await expect(groups).toHaveCount(1);
  await expect(groups.getByRole('tab')).toHaveCount(0);
  result.phase = 'open-two-owner-models';
  await command('_workbench.open', resources[0], [undefined, { pinned: true }]);
  await expect(editor(0).locator('.view-lines')).toContainText('alphaOwner');
  await command('_workbench.open', resources[1], [1, { pinned: true }]);
  await expect(groups).toHaveCount(2);
  await expect(editor(1).locator('.view-lines')).toContainText('betaOwner');
  await expect.poll(() => symbols(resources[0]), { timeout: 30_000 }).toEqual(['alphaOwner']);
  await expect.poll(() => symbols(resources[1]), { timeout: 30_000 }).toEqual(['betaOwner']);
  result.initial = await Promise.all(resources.map(symbols));
  result.initialTokens = [await inspectJsonToken(0, 'alphaOwner'), await inspectJsonToken(1, 'betaOwner')];
  for (const target of targets) {
    expect(result.reads.some(read => read.uri === target.rootUri + '/same.json'
      && read.workspaceId === target.workspaceId && read.gatewayId === target.gatewayId)).toBe(true);
  }
  result.phase = 'edit-real-model';
  await editor(0).getByRole('textbox').focus();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('{"alphaAfter":true}\n');
  await expect(editor(0).locator('.view-lines')).toContainText('alphaAfter');
  await expect(groups.nth(0)).toHaveClass(/\bactive\b/);
  await expect.poll(() => symbols(resources[0])).toEqual(['alphaAfter']);
  await expect.poll(() => symbols(resources[1])).toEqual(['betaOwner']);
  result.edited = await Promise.all(resources.map(symbols));
  result.editedToken = await inspectJsonToken(0, 'alphaAfter');
  await expect(editor(1).locator('.view-lines')).toContainText('betaOwner');
  result.phase = 'release-and-reopen';
  await command('workbench.action.revertAndCloseActiveEditor');
  await expect(groups).toHaveCount(1);
  await expect(editor(0).locator('.view-lines')).toContainText('betaOwner');
  await command('_workbench.open', resources[0], [1, { pinned: true }]);
  await expect(groups).toHaveCount(2);
  await expect(editor(1).locator('.view-lines')).toContainText('alphaOwner');
  await expect.poll(() => symbols(resources[0])).toEqual(['alphaOwner']);
  result.reopened = await symbols(resources[0]);
  result.reopenedToken = await inspectJsonToken(1, 'alphaOwner');
  await expect.poll(async () => (await renderedTokenClasses(1)).length).toBeGreaterThan(1);
  result.renderedTokens = { json: await renderedTokenClasses(1) };
  result.phase = 'language-mode-invalidation';
  await changeLanguage('plaintext', 'Plain Text');
  await expect.poll(() => symbols(resources[0])).toEqual([]);
  await expect.poll(() => symbols(resources[1])).toEqual(['betaOwner']);
  await expect.poll(async () => (await renderedTokenClasses(1)).length).toBe(1);
  result.renderedTokens.plainText = await renderedTokenClasses(1);
  await command('editor.action.inspectTMScopes');
  const plainTokens = editor(1).locator('.token-inspect-widget');
  await expect(plainTokens).toHaveText('No grammar or semantic tokens available.');
  result.plainTextToken = await plainTokens.textContent();
  await command('editor.action.inspectTMScopes');
  await expect(plainTokens).toHaveCount(0);
  await changeLanguage('json', 'JSON');
  await expect.poll(() => symbols(resources[0])).toEqual(['alphaOwner']);
  await expect.poll(() => symbols(resources[1])).toEqual(['betaOwner']);
  result.restoredLanguage = await Promise.all(resources.map(symbols));
  result.restoredToken = await inspectJsonToken(1, 'alphaOwner');
  await expect.poll(async () => (await renderedTokenClasses(1)).length).toBeGreaterThan(1);
  result.renderedTokens.restoredJson = await renderedTokenClasses(1);
  expect(result.renderedTokens.restoredJson).toEqual(result.renderedTokens.json);
  expect(result.pageErrors).toEqual([]);
  result.phase = 'complete';
} catch (error) {
  failure = error;
  result.error = String(error);
  if (page) {
    try {
      result.renderedDom = await page.locator('.editor-group-container .view-line').evaluateAll(lines => lines.map(line => line.innerHTML));
      await page.screenshot({ path: join(dirname(output), 'language-models-failure.png'), fullPage: true });
    }
    catch (diagnostic) { failure = new AggregateError([failure, diagnostic], '语言模型验收与截图失败'); }
  }
} finally {
  try { await writeFile(output, JSON.stringify(result, null, 2)); }
  catch (diagnostic) { failure = failure ? new AggregateError([failure, diagnostic], '语言模型结果保存失败') : diagnostic; }
  try { await run.close(); }
  catch (cleanup) { failure = failure ? new AggregateError([failure, cleanup], '语言模型验收与清理失败') : cleanup; }
}
if (failure) throw failure;
