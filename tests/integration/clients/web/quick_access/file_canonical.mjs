import { createHash } from 'node:crypto';
import { appendFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { readFile, rename, stat, utimes, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';
import { searchReceipt } from '../../../../support/workbench/search-observation.mjs';
import { writeWorkspaceSettings } from '../../../../support/workbench/settings.mjs';

const required = name => { if (!process.env[name]) throw new Error(`文件身份验收缺少 ${name}`); return process.env[name]; };
const output = required('BOXTEAM_BROWSER_RESULT_PATH'), screenshot = required('BOXTEAM_BROWSER_SCREENSHOT_PATH');
const nativeRoot = required('BOXTEAM_CANONICAL_WORKSPACE'), windows = process.platform === 'win32';
const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_file_canonical.py');
const result = { classification: 'Integration', phase: 'initialize', pageErrors: [], rawReads: [], references: [], receipts: [],
  boundary: '真实Gateway／后端／正式Workbench／原编辑器与JSON语言服务；原生文件仅由测试预备和修改，不预写浏览器存储、备份、模型或HTTP回执' };
let page, failure;
const pendingRequests = new Map();
let sequence = 0;
// 同步保存阶段与动作，进程被外部期限终止时仍保留最后卡点及真实网络活动。
const record = (action, state, details = {}) => {
  const event = { sequence: ++sequence, at: new Date().toISOString(), phase: result.phase, action, state, ...details };
  appendFileSync(output + '.stages.jsonl', JSON.stringify(event) + '\n');
  if (state !== 'observed') {
    result.lastAction = event;
    result.pendingRequests = [...pendingRequests.values()];
    writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(event));
  }
};
const observe = async (action, operation, details) => {
  record(action, 'start', details);
  const value = await operation();
  record(action, 'complete', details);
  return value;
};
const phase = name => { result.phase = name; record('phase', name === 'complete' ? 'complete' : 'start'); };
try {
  record('driver', 'start', { executable: process.execPath, version: process.version, platform: process.platform });
  const browser = await observe('browser.launch', () => chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }));
  run.addCleanup('canonical-browser', () => observe('browser.close', () => browser.close()));
  const context = await observe('browser.newContext', () => browser.newContext({ viewport: { width: 1900, height: 1150 } }));
  run.addCleanup('canonical-context', () => observe('context.close', () => context.close()));
  page = await observe('context.newPage', () => context.newPage());
  page.on('pageerror', error => { result.pageErrors.push(String(error)); record('pageerror', 'observed', { error: String(error) }); });
  page.on('request', request => {
    const url = new URL(request.url());
    const owner = { gatewayId: request.headers()['x-boxteam-target-gateway-id'], workspaceId: request.headers()['x-boxteam-workspace-id'] };
    if (url.pathname.startsWith('/api/')) {
      const details = { path: url.pathname, method: request.method(), owner };
      pendingRequests.set(request, details);
      record('http.request', 'observed', details);
      if (url.pathname === '/api/gateway/search') record('search.request', 'observed', { body: request.postDataJSON() });
    }
    if (url.pathname.endsWith('/workspace/files/raw')) result.rawReads.push({ phase: result.phase, uri: url.searchParams.get('path'), owner });
    if (url.pathname.endsWith('/workspace/files/references')) result.references.push({ phase: result.phase, ...request.postDataJSON(), owner });
  });
  page.on('response', response => {
    const request = response.request(), details = pendingRequests.get(request);
    if (details) record('http.response', 'observed', { ...details, status: response.status(), requestId: response.headers()['x-request-id'] });
  });
  page.on('requestfinished', request => pendingRequests.delete(request));
  page.on('requestfailed', request => {
    const details = pendingRequests.get(request);
    if (details) record('http.failed', 'observed', { ...details, error: request.failure()?.errorText });
    pendingRequests.delete(request);
  });
  const api = (path, init = {}) => observe('api', () => page.evaluate(async ({ path, init }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...init.headers } });
    const body = await response.json();
    if (!response.ok || body.code !== 0 || !body.request_id || body.request_id !== response.headers.get('X-Request-ID')) {
      throw new Error(JSON.stringify({ path, status: response.status, body }));
    }
    return body.data;
  }, { path, init }), { path, method: init.method ?? 'GET' });
  const headers = target => ({ 'X-BoxTeam-Target-Gateway-Id': target.gateway_id, 'X-BoxTeam-Workspace-Id': target.workspace_id });
  await observe('gateway.health.navigation', () => page.goto(new URL('/api/gateway/health', required('BOXTEAM_BROWSER_BASE_URL')).href));
  await api('/api/gateway/users/guest', { method: 'POST', body: JSON.stringify({ tracking: { source: 'file-canonical' } }) });
  const health = await api('/api/gateway/health'), listing = await api('/api/gateway/workspaces');
  const active = listing.active_workspace;
  expect(active.gateway_id).toBe(health.gateway_id);
  const managed = await api('/api/gateway/managed-workspaces', { method: 'POST', body: JSON.stringify({
    root_path: required('BOXTEAM_CANONICAL_OTHER'), name: 'Canonical B',
  }) });
  const other = managed.items.find(item => item.name === 'Canonical B');
  expect(other).toBeTruthy();
  await api(`/api/gateway/workspaces/${active.workspace_id}/activate`, { method: 'POST', headers: headers(active) });
  const targets = [];
  for (const target of [active, other]) {
    const workspace = await api('/api/v1/workspace', { headers: headers(target) });
    const roots = await api('/api/v1/workspace/files/roots', { headers: headers(target) });
    expect(workspace.workspace_id).toBe(target.workspace_id);
    targets.push({ gatewayId: target.gateway_id, workspaceId: target.workspace_id, rootUri: roots.workspace_uri });
  }
  const session = await api('/api/v1/sessions', { method: 'POST', headers: headers(active), body: JSON.stringify({ title: '文件规范身份验收' }) });
  result.targets = targets; result.sessionId = session.session_id;
  await observe('workbench.navigation', () => page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' }));
  await observe('workbench.navigation.visible', () => expect(page.locator('.native-session-navigation')).toBeVisible({ timeout: 30_000 }));
  const command = (id, ...args) => observe('command', () => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args }), { command: id });
  const quick = page.locator('.quick-input-widget'), groups = page.locator('.editor-group-container');
  const editor = () => page.locator('.editor-instance .monaco-editor').filter({ visible: true }).last();
  const docTabs = () => groups.getByRole('tab', { name: /(?:Doc|DOC)\.json/ });
  const uri = (target, path, root = target.rootUri) => page.evaluate(({ target, path, root }) => globalThis.workbench.URI.from({
    scheme: 'boxteam-file', authority: target.gatewayId, path: `/${target.workspaceId}/${encodeURIComponent(root)}/${path}`,
  }).toString(), { target, path, root });
  const symbols = resource => observe('document.symbols', () => page.evaluate(async resource => {
    const items = await globalThis.workbench.commands.executeCommand('_executeDocumentSymbolProvider', globalThis.workbench.URI.parse(resource));
    return items.map(item => item.name);
  }, resource), { resource });
  const openInternal = resource => command('_workbench.open', resource, [undefined, { pinned: true }]);
  const setText = async text => {
    const input = editor().getByRole('textbox');
    await input.focus(); await input.press('ControlOrMeta+A');
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.evaluate(text => navigator.clipboard.writeText(text), text);
    await input.press('ControlOrMeta+V');
    await expect(editor().locator('.view-lines')).toContainText(JSON.parse(text) && Object.keys(JSON.parse(text))[0]);
  };
  const openLink = async publicUri => {
    await command('workbench.action.quickOpen', publicUri);
    await expect(quick.locator('input')).toHaveValue(publicUri);
    const [response, reference] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/gateway/resource-addresses/parse'
        && response.request().postDataJSON()?.uri === publicUri),
      page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/workspace/files/references')
        && response.request().headers()['x-boxteam-target-gateway-id'] === targets[0].gatewayId
        && response.request().headers()['x-boxteam-workspace-id'] === targets[0].workspaceId),
      quick.locator(`[data-quick-input-id="link:${publicUri}"]`).click(),
    ]);
    const body = await response.json();
    expect(response.status()).toBe(200); expect(body.request_id).toBe(response.headers()['x-request-id']);
    expect(body.data.gateway_id).toBe(targets[0].gatewayId); expect(body.data.scope_id).toBe(targets[0].workspaceId);
    // 原 Opener 仍须完成 owner 路由与规范化；只等解析回执会被旧脏模型的正文断言提前放行。
    await reference.finished();
    const canonicalization = await reference.json();
    expect(reference.status()).toBe(200); expect(canonicalization.code).toBe(0);
    expect(canonicalization.request_id).toBe(reference.headers()['x-request-id']);
    expect(reference.request().postDataJSON().paths).toEqual([body.data.uri]);
    expect(canonicalization.data.uris).toEqual([targets[0].rootUri + '/Mixed/Doc.json']);
    result.receipts.push({ publicUri, body, canonicalization });
    await expect(quick).not.toBeVisible();
    await expect(editor().getByRole('textbox')).toBeFocused();
  };
  // 只读原 IndexedDB 中实际写入的备份，不替产品生成或修改任何恢复记录。
  const backups = () => page.evaluate(async () => {
    const database = await new Promise((accept, reject) => {
      const request = indexedDB.open('vscode-web-db');
      request.onupgradeneeded = () => { request.transaction.abort(); reject(new Error('原备份数据库尚未创建')); };
      request.onerror = () => reject(request.error); request.onsuccess = () => accept(request.result);
    });
    try {
      return await new Promise((accept, reject) => {
        const transaction = database.transaction('vscode-userdata-store'), cursor = transaction.objectStore('vscode-userdata-store').openCursor();
        const values = [];
        cursor.onsuccess = () => {
          const item = cursor.result;
          if (!item) return;
          if (String(item.key).includes('/Backups/')) {
            const content = new TextDecoder().decode(item.value), end = content.indexOf('\n'), preamble = content.slice(0, end), separator = preamble.indexOf(' ');
            values.push({ key: String(item.key), resource: preamble.slice(0, separator), meta: JSON.parse(preamble.slice(separator + 1)), content: content.slice(end + 1) });
          }
          item.continue();
        };
        transaction.oncomplete = () => accept(values); transaction.onerror = () => reject(transaction.error); transaction.onabort = () => reject(transaction.error);
      });
    } finally { database.close(); }
  });
  await observe('settings.disableAutoSave', () => writeWorkspaceSettings({ page, command }, { 'files.autoSave': 'off' }));
  await command('workbench.action.closeAllEditors');
  phase('tree-context-menu-real-editor');
  await observe('tree.openThread', () => page.locator(`button[data-session-id="${session.session_id}"]`).dblclick());
  const thread = page.locator(`section.boxteam-thread[data-session-id="${session.session_id}"]`);
  await observe('tree.thread.visible', () => expect(thread).toBeVisible());
  const toggle = thread.getByRole('button', { name: '切换会话资源', exact: true });
  if (await toggle.getAttribute('aria-expanded') !== 'true') await observe('tree.expandResources', () => toggle.click());
  await observe('tree.filesTab', () => thread.getByRole('tab', { name: '文件', exact: true }).click());
  const workspaceRoot = thread.getByTitle(targets[0].rootUri, { exact: true }).and(thread.locator('button.root'));
  if (await workspaceRoot.getAttribute('aria-expanded') !== 'true') await observe('tree.expandWorkspace', () => workspaceRoot.click());
  const mixed = thread.getByTitle(targets[0].rootUri + '/Mixed', { exact: true }).and(thread.locator('button.files-tree-item'));
  await observe('tree.expandMixed', () => mixed.click());
  const fileRow = thread.getByTitle(targets[0].rootUri + '/Mixed/Doc.json', { exact: true }).and(thread.locator('button.files-tree-item'));
  await observe('tree.file.contextMenu', () => fileRow.click({ button: 'right' }));
  await observe('tree.file.vscodeMenu', () => thread.getByRole('menuitem', { name: 'VS Code', exact: true }).click());
  await observe('tree.file.openEditor', () => page.getByRole('menuitem', { name: '在编辑器中打开', exact: true }).click({ delay: 120 }));
  await observe('tree.file.diskVisible', () => expect(editor().locator('.view-lines')).toContainText('diskOwner'));
  const canonical = await uri(targets[0], 'Mixed/Doc.json');
  await setText('{"savedBase":1}\n');
  await expect(docTabs()).toHaveCount(1); await expect(docTabs()).toHaveClass(/dirty/);
  const beforeReopen = result.rawReads.length;
  phase('public-link-same-dirty-model');
  const qualifiedRoot = targets[0].rootUri.replace('boxteam://', `boxteam://${targets[0].gatewayId}/`);
  await openLink(qualifiedRoot + (windows ? '/mIXED/dOC.json' : '/Mixed/Doc.json'));
  await expect(editor().locator('.view-lines')).toContainText('savedBase'); await expect(docTabs()).toHaveCount(1);
  phase('federated-candidate-same-dirty-model');
  const response = page.waitForRequest(request => new URL(request.url()).pathname === '/api/gateway/search'
    && request.postDataJSON()?.queries.some(query => query.category === 'file' && query.query === 'Doc'));
  await command('workbench.action.quickOpen', 'Doc');
  const receipt = await searchReceipt(await response);
  expect(receipt.status).toBe(200);
  expect(receipt.scope.owners).toEqual([{ gateway_id: targets[0].gatewayId, workspace_id: targets[0].workspaceId }]);
  const entries = receipt.pages.filter(event => event.page.files).flatMap(event => event.page.files.entries);
  expect(entries.some(entry => entry.canonical_uri === targets[0].rootUri + '/Mixed/Doc.json')).toBe(true);
  await expect(quick).toContainText('文件 · 本次查询完成');
  await quick.locator(`[data-quick-input-id="${canonical}"]`).click();
  await expect(quick).not.toBeVisible(); await expect(docTabs()).toHaveCount(1);
  await expect(editor().locator('.view-lines')).toContainText('savedBase');
  phase('direct-original-model-resolver-alias');
  const aliasIdentity = await uri(targets[0], windows ? 'mixed/doc.json' : 'Mixed/Doc.json', qualifiedRoot);
  expect(await symbols(aliasIdentity)).toEqual(['savedBase']);
  expect(result.rawReads.length).toBe(beforeReopen);
  result.threeEntriesOneDirtyModel = true; result.directResolverUsesDirtyModel = true;
  phase('same-path-other-owner-and-logical-link');
  const ownerB = await uri(targets[1], 'Mixed/Doc.json'), link = await uri(targets[0], 'Alias/Doc.json');
  await openInternal(ownerB); await expect(editor().locator('.view-lines')).toContainText('otherOwner');
  await expect(docTabs()).toHaveCount(2); expect(await symbols(canonical)).toEqual(['savedBase']); expect(await symbols(ownerB)).toEqual(['otherOwner']);
  await openInternal(link); await expect(editor().locator('.view-lines')).toContainText('diskOwner');
  await expect(docTabs()).toHaveCount(3); expect(await symbols(link)).toEqual(['diskOwner']); expect(await symbols(canonical)).toEqual(['savedBase']);
  result.completeOwnerIsolation = true; result.logicalLinkIndependent = true;
  await command('workbench.action.revertAndCloseActiveEditor'); await openInternal(ownerB); await command('workbench.action.revertAndCloseActiveEditor');
  phase('mixed-directory-case-sensitive-models');
  const upper = await uri(targets[0], 'Mixed/Sensitive/Case.json'), lower = await uri(targets[0], 'Mixed/Sensitive/case.json');
  await openInternal(upper); await expect(editor().locator('.view-lines')).toContainText('caseUpper');
  await openInternal(lower); await expect(editor().locator('.view-lines')).toContainText('caseLower');
  expect(await symbols(upper)).toEqual(['caseUpper']); expect(await symbols(lower)).toEqual(['caseLower']);
  expect(await groups.getByRole('tab', { name: /[Cc]ase\.json/ }).count()).toBe(2);
  result.mixedSensitivityIndependent = true;
  await command('workbench.action.revertAndCloseActiveEditor'); await openInternal(upper); await command('workbench.action.revertAndCloseActiveEditor');
  await openInternal(canonical);
  phase('normal-dirty-reload');
  await command('workbench.action.pinEditor');
  await command('workbench.action.quickOpen', ':1:4'); await page.keyboard.press('Enter');
  await expect.poll(async () => (await backups()).find(backup => backup.resource === canonical)?.content).toBe('{"savedBase":1}\n');
  const originalBackup = (await backups()).find(backup => backup.resource === canonical);
  expect(originalBackup.meta.boxteamFileRevision).toBe(createHash('sha256').update('{"diskOwner":1}\n').digest('hex'));
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  const beforeReloadReads = result.rawReads.length;
  await observe('workbench.normalReload', () => page.reload({ waitUntil: 'domcontentloaded' }));
  await expect(editor().locator('.view-lines')).toContainText('savedBase'); await expect(docTabs()).toHaveCount(1); await expect(docTabs()).toHaveClass(/dirty/);
  await expect(docTabs()).toHaveClass(/sticky/); await expect(page.locator('#status\\.editor\\.selection')).toContainText('Ln 1, Col 4');
  expect(result.rawReads.length).toBe(beforeReloadReads);
  const saved = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname.endsWith('/workspace/files/content'));
  await command('workbench.action.files.save');
  const savedResponse = await saved;
  expect(savedResponse.status()).toBe(200);
  expect(savedResponse.request().postDataJSON().expected_revision).toBe(originalBackup.meta.boxteamFileRevision);
  expect(await readFile(join(nativeRoot, 'Mixed/Doc.json'), 'utf8')).toBe('{"savedBase":1}\n');
  await expect(docTabs()).not.toHaveClass(/dirty/); result.normalReloadSaved = true;
  phase('dirty-reload-with-external-change');
  const draft = '{\n  "renameDirty": 2\n}\n';
  await setText(draft); await command('workbench.action.quickOpen', ':2:4'); await page.keyboard.press('Enter');
  await expect.poll(async () => (await backups()).find(backup => backup.resource === canonical)?.content).toBe(draft);
  const pendingBackup = (await backups()).find(backup => backup.resource === canonical);
  expect(pendingBackup.meta.boxteamFileRevision).toBe(createHash('sha256').update('{"savedBase":1}\n').digest('hex'));
  const currentName = windows ? 'mIXED/DOC.json' : 'Mixed/Doc.json', current = await uri(targets[0], currentName);
  await page.evaluate(() => globalThis.boxteamWorkbenchLifecycle.prepareToClose());
  await observe('workbench.leaveBeforeExternalChange', () => page.goto(new URL('/api/gateway/health', required('BOXTEAM_BROWSER_BASE_URL')).href));
  if (windows) {
    await rename(join(nativeRoot, 'Mixed/Doc.json'), join(nativeRoot, 'Mixed/DOC.json'));
    await rename(join(nativeRoot, 'Mixed'), join(nativeRoot, 'mIXED'));
  }
  result.caseOnlyRename = windows;
  const currentFile = join(nativeRoot, currentName);
  const savedStat = await stat(currentFile);
  await writeFile(currentFile, '{"savedBase":2}\n');
  // 保留同长正文并回拨 mtime，让本次冲突必须由原 SHA CAS 判定。
  await utimes(currentFile, savedStat.atime, new Date(savedStat.mtimeMs - 1000));
  const beforeConflictReloadReads = result.rawReads.length;
  await observe('workbench.restoreAfterExternalChange', () => page.goto(required('BOXTEAM_BROWSER_BASE_URL'), { waitUntil: 'domcontentloaded' }));
  await expect(editor().locator('.view-lines')).toContainText('renameDirty'); await expect(docTabs()).toHaveCount(1); await expect(docTabs()).toHaveClass(/dirty/);
  await expect(docTabs()).toHaveClass(/sticky/); await expect(page.locator('#status\\.editor\\.selection')).toContainText('Ln 2, Col 4');
  await expect.poll(() => symbols(current), { timeout: 30_000 }).toEqual(['renameDirty']);
  expect(await symbols(aliasIdentity)).toEqual(['renameDirty']);
  expect(result.rawReads.length).toBe(beforeConflictReloadReads);
  const restoredBackup = (await backups()).find(backup => backup.resource === current);
  expect(restoredBackup.content).toBe(draft); expect(restoredBackup.meta.boxteamFileRevision).toBe(pendingBackup.meta.boxteamFileRevision);
  if (windows) expect((await backups()).some(backup => backup.resource === canonical)).toBe(false);
  const conflict = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname.endsWith('/workspace/files/content'));
  await command('workbench.action.files.save');
  const rejected = await conflict;
  expect(rejected.status()).toBe(409);
  expect(rejected.request().postDataJSON().expected_revision).toBe(pendingBackup.meta.boxteamFileRevision);
  const rejection = await rejected.json(); expect(rejection.request_id).toBe(rejected.headers()['x-request-id']);
  await expect(editor().locator('.view-lines')).toContainText('renameDirty'); await expect(docTabs()).toHaveClass(/dirty/);
  const notification = page.locator('.notifications-toasts');
  await expect(notification).toContainText('The content of the file is newer.');
  await expect(notification.getByRole('button', { name: 'Compare', exact: true })).toBeVisible();
  await expect(notification.getByRole('button', { name: 'Overwrite', exact: true })).toBeVisible();
  result.conflictNotification = await notification.innerText();
  expect(await readFile(currentFile, 'utf8')).toBe('{"savedBase":2}\n');
  result.restoredConflictPreserved = true; result.backups = { originalBackup, pendingBackup, restoredBackup }; result.conflict = rejection;
  expect(result.pageErrors).toEqual([]); phase('complete');
} catch (error) {
  failure = error; result.error = String(error?.stack ?? error);
  record('scenario', 'failed', { error: result.error });
  if (page) try { await page.screenshot({ path: screenshot, fullPage: true }); } catch (capture) { result.screenshotError = String(capture); }
} finally {
  try { await writeFile(output, JSON.stringify(result, null, 2) + '\n'); } catch (error) { failure = failure ? new AggregateError([failure, error], '文件身份验收及证据写入均失败') : error; }
  try { await run.close(); } catch (error) { failure = failure ? new AggregateError([failure, error], '文件身份验收及清理均失败') : error; }
}
if (failure) throw failure;
