// Integration：原版 Profile／FolderConfiguration；故障变体只延迟原 IDB provider 的一个实际写入。
import { createHash } from 'node:crypto';
import { cp, readFile, writeFile } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';
import { chromium } from 'playwright';
import { expect } from 'playwright/test';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

function check(value, message) { if (!value) throw new Error(message); }
function required(name) { const value = process.env[name]; check(value, `缺少环境变量 ${name}`); return value; }
const digest = value => createHash('sha256').update(value).digest('hex');

async function prepareAssets(original, destination, artifacts) {
  const { default: ts } = await import('../../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js');
  const metadata = JSON.parse(await readFile(join(original, 'build-meta.json'), 'utf8'));
  const outputs = Object.entries(metadata.outputs).filter(([, output]) => Object.keys(output.inputs)
    .some(input => input.endsWith('/indexedDBFileSystemProvider.ts')));
  check(outputs.length === 1, '正式产物必须只有一个 IDB provider 输出');
  const input = resolve('src/clients/workbench', outputs[0][0]);
  const file = relative(original, input);
  check(file && !file.startsWith('..'), 'IDB provider 输出不在正式资产内');
  const source = await readFile(input, 'utf8');
  // 正式主包很大；唯一构造器锚点定位原 provider，只完整解析包含它的类。
  const marker = 'this.writeManyThrottler=new';
  check(source.split(marker).length === 2, '原 IDB provider 构造器锚点必须唯一');
  const markerOffset = source.indexOf(marker), classStart = source.lastIndexOf('=class ', markerOffset) + 1;
  check(classStart > 0, '原 IDB provider class 起点不匹配');
  const prefix = 'const ConfigurationCacheIdbFixture=', window = source.slice(classStart, classStart + 32768);
  const ast = ts.createSourceFile(file, prefix + window,
    ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const provider = ast.statements[0]?.declarationList?.declarations[0]?.initializer;
  check(provider && ts.isClassExpression(provider) && provider.end < prefix.length + window.length
    && !ast.parseDiagnostics.some(error => error.start < provider.end), '必须取得未截断的原 provider class AST');
  const constructors = provider.members.filter(ts.isConstructorDeclaration);
  const methods = provider.members.filter(member => member.name?.getText(ast) === 'writeFile');
  check(constructors.length === 1 && constructors[0].body.getText(ast).includes(marker)
    && methods.length === 1, '原 IDB provider constructor／writeFile 必须在同一类中唯一');
  const method = methods[0], originalClass = source.slice(classStart, classStart + provider.end - prefix.length);
  const methodStart = classStart + method.getStart(ast) - prefix.length;
  check(originalClass === provider.getText(ast)
    && source.slice(methodStart, classStart + method.end - prefix.length) === method.getText(ast)
    && method.parameters.length === 3
    && method.body.getText(ast).includes('this.bulkWrite('), '原 IDB writeFile 方法体不匹配');
  const [resource, content] = method.parameters.map(parameter => parameter.name.getText(ast));
  // 插入点在原方法体内，不变异原型；精确路径由真实产品首次写出的缓存观察得到。
  const insertion = `\nif(this.scheme==="vscode-userdata"&&globalThis.__configurationCacheWriteGate?.path===${resource}.path){
const gate=globalThis.__configurationCacheWriteGate;
const text=new TextDecoder().decode(${content});
if(!gate.entered&&JSON.parse(JSON.parse(text).content.settings)["editor.fontSize"]===gate.fontSize){
gate.entered={resource:${resource}.toString(),path:${resource}.path,content:text,at:Date.now()};
await new Promise(release=>{gate.release=release;});gate.resumed=true;
}}
`;
  const offset = classStart + method.body.getStart(ast) + 1 - prefix.length;
  const modified = source.slice(0, offset) + insertion + source.slice(offset);
  check(modified.slice(0, offset) + modified.slice(offset + insertion.length) === source, '插桩必须只是唯一插入');
  await cp(original, destination, { recursive: true, errorOnExist: true, force: false });
  await writeFile(join(destination, file), modified);
  await writeFile(join(artifacts, 'instrumentation.diff.json'), JSON.stringify({
    boundary: '测试独占副本；原 IDB provider writeFile 入口精确屏障，放行后继续原方法',
    file, offset_utf16: offset, delete_utf16: 0, insert_utf8: insertion,
    original_sha256: digest(source), instrumented_sha256: digest(modified),
    original_class_sha256: digest(originalClass), class_start_utf16: classStart, method_start_utf16: methodStart,
    original_method: method.getText(ast),
    upstream_provider_sha256: digest(await readFile('src/clients/workbench/upstream/src/vs/platform/files/browser/indexedDBFileSystemProvider.ts')),
  }, null, 2) + '\n');
}

async function runBrowser() {
  const fixture = JSON.parse(required('BOXTEAM_BROWSER_FIXTURE'));
  const base = required('BOXTEAM_BROWSER_BASE_URL');
  const result = { classification: 'Integration', variant: fixture.variant, phase: 'prepare',
    boundary: fixture.variant === 'original' ? '未插桩正式 Workbench，真实 Gateway／后端与 Profile' :
      '独占正式资产副本的原 IDB writeFile 精确屏障；非发行构建 E2E',
    providerBoundary: 'model_stream 精确回放，不应产生模型调用',
    routeStubs: [], pageErrors: [], consoleErrors: [], network: [], closes: [], profileObservations: [] };
  const run = TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/test_configuration_cache.py');
  let page, failure;
  const profileName = 'Configuration Cache Profile B';
  const command = (id, ...args) => page.evaluate(async ({ id, args }) => {
    await globalThis.workbench.commands.executeCommand(id, ...args);
  }, { id, args });
  const api = (path, owner) => page.evaluate(async ({ path, owner }) => {
    const token = (await (await fetch('/api/gateway/auth/local-credential')).json()).data.token;
    const response = await fetch(path, { headers: { 'X-Local-Token': token,
      ...(owner ? { 'X-BoxTeam-Workspace-Id': owner.workspaceId, 'X-BoxTeam-Target-Gateway-Id': owner.gatewayId } : {}) } });
    const payload = await response.json();
    if (!response.ok || payload.request_id !== response.headers.get('X-Request-ID')) throw new Error(JSON.stringify({ path, payload }));
    return payload;
  }, { path, owner });
  async function readStore(databaseName, storeName) {
    return page.evaluate(async ({ databaseName, storeName }) => {
      const database = await new Promise((resolve, reject) => {
        const request = indexedDB.open(databaseName);
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
        request.onupgradeneeded = () => request.transaction.abort();
      });
      try {
        return await new Promise((resolve, reject) => {
          const entries = [], transaction = database.transaction(storeName, 'readonly');
          const request = transaction.objectStore(storeName).openCursor();
          request.onsuccess = () => {
            if (!request.result) return;
            entries.push({ path: String(request.result.key), text: typeof request.result.value === 'string'
              ? request.result.value : new TextDecoder().decode(request.result.value) });
            request.result.continue();
          };
          transaction.oncomplete = () => resolve(entries);
          transaction.onerror = () => reject(transaction.error); transaction.onabort = () => reject(transaction.error);
        });
      } finally { database.close(); }
    }, { databaseName, storeName });
  }
  const userEntries = () => readStore('vscode-web-db', 'vscode-userdata-store');
  const folderCaches = async () => (await userEntries()).filter(entry => /\/CachedConfigurations\/folder\//.test(entry.path));
  async function cacheAt(fontSize, path) {
    let matched;
    await expect.poll(async () => {
      matched = (await folderCaches()).filter(entry => (!path || entry.path === path)
        && JSON.parse(JSON.parse(entry.text).content.settings)['editor.fontSize'] === fontSize);
      return matched.length;
    }).toBe(1);
    return matched[0];
  }
  async function protectedSettings() {
    return {
      user: (await userEntries()).filter(entry => /\/settings\.json$/.test(entry.path)),
      local: (await readStore('boxteam-workbench-files', 'files')).filter(entry => entry.path === '/Workspace/.vscode/settings.json'),
    };
  }
  async function openSettings(id) {
    await command(id);
    const dialog = page.getByRole('dialog', { name: /settings\.json$/ });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('.view-lines')).toBeVisible();
    return dialog;
  }
  async function saveSettings(id, settings) {
    const dialog = await openSettings(id), editor = dialog.locator('.monaco-editor [role="textbox"]');
    await editor.focus(); await editor.press('ControlOrMeta+A');
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.evaluate(text => navigator.clipboard.writeText(text), JSON.stringify(settings, null, 2) + '\n');
    await editor.press('ControlOrMeta+V');
    await expect(dialog.locator('.view-lines')).toContainText(Object.keys(settings).at(-1));
    await command('workbench.action.files.save');
    await command('workbench.action.closeActiveEditor'); await expect(dialog).not.toBeVisible();
  }
  async function verifySettings(fontSize, lineHeight) {
    const dialog = await openSettings('workbench.action.openWorkspaceSettingsFile');
    await expect(dialog.locator('.view-lines')).toContainText(`"editor.fontSize": ${fontSize}`);
    const line = dialog.locator('.view-line').first();
    await expect.poll(() => line.evaluate(element => getComputedStyle(element).fontSize)).toBe(`${fontSize}px`);
    await expect.poll(() => line.evaluate(element => getComputedStyle(element).lineHeight)).toBe(`${lineHeight}px`);
    await command('workbench.action.closeActiveEditor'); await expect(dialog).not.toBeVisible();
  }
  async function switchProfile(name) {
    const switched = command('workbench.profiles.actions.switchProfile');
    const quick = page.locator('.quick-input-widget'); await expect(quick).toBeVisible();
    await quick.getByText(name, { exact: true }).click(); await switched;
    await expect(quick).not.toBeVisible();
    result.profileObservations.push(await page.evaluate(() => ({
      profiles: JSON.parse(localStorage.getItem('userDataProfiles') || '[]'),
      associations: JSON.parse(localStorage.getItem('profileAssociations') || '{}'),
    })));
  }
  async function createProfile() {
    await command('workbench.profiles.actions.createProfile');
    await page.getByRole('textbox', { name: 'Profile Name', exact: true }).fill(profileName);
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Use this Profile for Current Window', exact: true })).toBeVisible();
    const stored = await page.evaluate(name => JSON.parse(localStorage.getItem('userDataProfiles') || '[]').find(profile => profile.name === name), profileName);
    check(stored?.location?.path, 'Profile 持久化记录必须包含原资源 location');
    result.namedProfile = stored;
    await command('workbench.action.closeActiveEditor');
    await switchProfile(profileName);
    const profileId = result.profileObservations.at(-1).associations.workspaces['workbench:/Workspace'];
    check(profileId && stored.location.path.endsWith('/' + profileId), '原 Profile 关联 ID 必须匹配持久化 location');
    result.namedProfile.id = profileId;
  }
  const ownedContent = owner => api('/api/v1/workspace/files/content?' + new URLSearchParams({
    scope: 'workspace', path: owner.root + '/.vscode/settings.json',
  }), owner);
  async function saveFolder(owner, fontSize) {
    await saveSettings('workbench.action.openWorkspaceSettingsFile', { 'editor.fontSize': fontSize, 'files.autoSave': 'off' });
    const response = await ownedContent(owner);
    expect(JSON.parse(response.data.content)['editor.fontSize']).toBe(fontSize);
    result.network.push({ phase: result.phase, owner, requestId: response.request_id, settings: JSON.parse(response.data.content) });
  }
  function attachPage(next) {
    next.on('pageerror', error => result.pageErrors.push(String(error)));
    next.on('console', message => { if (message.type() === 'error') result.consoleErrors.push({ phase: result.phase, text: message.text() }); });
    return next;
  }
  const folderUrl = folder => { const url = new URL('/', base); if (folder) url.searchParams.set('folder', folder); return url.href; };
  async function navigate(folder) {
    await page.goto(folderUrl(folder), { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#workbench .monaco-workbench')).toBeVisible();
    await expect(page.getByRole('button', { name: '刷新资源树', exact: true })).toBeVisible({ timeout: 30_000 });
  }
  async function closeReopen(context, folder, pending = false) {
    const closing = page;
    const record = { phase: result.phase, url: closing.url(), pending, mode: 'page.close(runBeforeUnload: true)' };
    result.closes.push(record);
    await Promise.all([closing.waitForEvent('close'), closing.close({ runBeforeUnload: true })]);
    record.closed = true;
    page = attachPage(await context.newPage()); await navigate(folder);
  }
  async function arm(path, fontSize) {
    await page.evaluate(({ path, fontSize }) => {
      globalThis.__configurationCacheWriteGate = { path, fontSize, entered: null, resumed: false };
    }, { path, fontSize });
  }
  async function entered() {
    await expect.poll(() => page.evaluate(() => globalThis.__configurationCacheWriteGate.entered)).not.toBeNull();
    return page.evaluate(() => globalThis.__configurationCacheWriteGate.entered);
  }
  try {
    const browser = await chromium.launch({ executablePath: required('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'), headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    run.addCleanup('ConfigurationCache Chromium', () => browser.close());
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    run.addCleanup('ConfigurationCache context', () => context.close());
    page = attachPage(await context.newPage()); await navigate();
    const health = await api('/api/gateway/health'), workspaces = await api('/api/gateway/workspaces');
    expect(health.data.gateway_id).toBe(fixture.owners.A.gatewayId);
    expect(workspaces.data.active_workspace.workspace_id).toBe(fixture.owners.A.workspaceId);
    result.initialization = { health, workspaces, metadata: await api('/api/v1/workspace', fixture.owners.A) };
    result.phase = 'local-settings-and-real-profile';
    await saveSettings('workbench.action.openSettingsJson', { 'editor.lineHeight': 31, 'files.autoSave': 'off' });
    await saveSettings('workbench.action.openWorkspaceSettingsFile', { 'editor.fontSize': 15, 'files.autoSave': 'off' });
    await verifySettings(15, 31);
    expect(await folderCaches()).toEqual([]);
    await createProfile();
    await saveSettings('workbench.action.openSettingsJson', { 'editor.lineHeight': 39, 'files.autoSave': 'off' });
    await verifySettings(15, 39);
    await switchProfile('Default'); await verifySettings(15, 31);
    const protectedBefore = await protectedSettings();
    const profileSettings = protectedBefore.user.filter(entry => entry.path.startsWith(result.namedProfile.location.path + '/'));
    expect(profileSettings).toHaveLength(1);
    expect(JSON.parse(profileSettings[0].text)['editor.lineHeight']).toBe(39);
    expect(protectedBefore.user).toHaveLength(2);
    expect(await folderCaches()).toEqual([]);
    result.localSettingsNotDuplicated = true; result.profileSettingsIsolated = true;
    const folders = {};
    for (const [label, owner] of Object.entries(fixture.owners)) folders[label] = await page.evaluate(owner =>
      globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: owner.gatewayId,
        path: `/${owner.workspaceId}/${encodeURIComponent(owner.root)}` }).toString(), owner);
    result.folderUris = folders;
    result.phase = 'nonlocal-A-and-profile';
    await navigate(folders.A); await verifySettings(17, 31);
    await saveFolder(fixture.owners.A, 19); const cacheA = await cacheAt(19);
    await switchProfile(profileName); await verifySettings(19, 39);
    await saveFolder(fixture.owners.A, 20); await cacheAt(20, cacheA.path);
    result.phase = 'normal-close-and-reopen';
    await closeReopen(context, folders.A); await verifySettings(20, 39);
    result.normalCloseReopened = true;
    result.phase = 'nonlocal-B-and-resource-roundtrip';
    await navigate(folders.B); await verifySettings(23, 31);
    await saveFolder(fixture.owners.B, 24); const cacheB = await cacheAt(24);
    expect(cacheB.path).not.toBe(cacheA.path);
    await switchProfile(profileName); await verifySettings(24, 39);
    await navigate(folders.A); await verifySettings(20, 39);
    await cacheAt(20, cacheA.path); await cacheAt(24, cacheB.path);
    result.folderCaches = { A: cacheA.path, B: cacheB.path }; result.folderRoundTrip = true;
    if (fixture.variant === 'delayed-cache-write') {
      result.phase = 'late-write-after-profile-change';
      await arm(cacheA.path, 21); await saveFolder(fixture.owners.A, 21);
      result.firstDelayedWrite = await entered();
      expect(result.firstDelayedWrite.path).toBe(cacheA.path);
      await switchProfile('Default'); await verifySettings(21, 31);
      await saveFolder(fixture.owners.A, 22);
      expect(await page.evaluate(() => globalThis.__configurationCacheWriteGate.resumed)).toBe(false);
      await cacheAt(20, cacheA.path);
      await page.evaluate(() => globalThis.__configurationCacheWriteGate.release());
      await cacheAt(22, cacheA.path); await cacheAt(24, cacheB.path); await verifySettings(22, 31);
      result.lateWriteKeepsResource = true;
      result.phase = 'normal-close-with-cache-write-pending';
      await arm(cacheA.path, 23); await saveFolder(fixture.owners.A, 23);
      const pending = await entered(); await cacheAt(22, cacheA.path);
      result.pendingClose = { entered: pending, cachedBeforeClose: 22,
        contract: '可再生缓存无需全部 drain；重开必须从原 owner 取得适用配置' };
      expect(await page.evaluate(() => globalThis.__configurationCacheWriteGate.resumed)).toBe(false);
      await closeReopen(context, folders.A, true);
      result.pendingClose.closedWithWritePending = true;
      await verifySettings(23, 31); await cacheAt(23, cacheA.path); await cacheAt(24, cacheB.path);
      const restored = await ownedContent(fixture.owners.A);
      expect(JSON.parse(restored.data.content)['editor.fontSize']).toBe(23);
      result.pendingClose.ownerRequestId = restored.request_id; result.pendingClose.rebuiltFromOwner = true;
    }
    expect(await protectedSettings()).toEqual(protectedBefore);
    result.protectedSettingsRetained = true;
    check(result.pageErrors.length === 0, `页面异常：${JSON.stringify(result.pageErrors)}`);
    const storageErrors = result.consoleErrors.filter(entry => /IndexedDB|database.*closed/i.test(entry.text));
    check(storageErrors.length === 0, `存储异常：${JSON.stringify(storageErrors)}`);
    result.phase = 'complete';
  } catch (error) {
    failure = error; result.error = String(error); throw error;
  } finally {
    const failures = [];
    if (page && !page.isClosed()) {
      try { result.pageText = await page.locator('body').innerText({ timeout: 5000 }); } catch (error) { failures.push(error); }
      try { await page.screenshot({ path: required('BOXTEAM_BROWSER_SCREENSHOT_PATH'), fullPage: true, timeout: 5000 }); }
      catch (error) { failures.push(error); }
    }
    try { await run.close(); } catch (error) { failures.push(error); }
    result.diagnosticErrors = failures.map(String);
    try { await writeFile(required('BOXTEAM_BROWSER_RESULT_PATH'), JSON.stringify(result, null, 2) + '\n'); }
    catch (error) { failures.push(error); }
    if (failures.length) throw new AggregateError(failure ? [failure, ...failures] : failures, '配置缓存场景、诊断或清理失败');
  }
}

if (process.argv[2] === '--prepare-assets') await prepareAssets(...process.argv.slice(3));
else await runBrowser();
