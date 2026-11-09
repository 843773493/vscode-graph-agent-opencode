import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/clients/web/workspace_files/workspace_file_dialog.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const bundle = join(run.artifactsDir, 'workspace-file-dialog.fixture.mjs');
const sourcePaths = [testFile, ...['scripts/sourceTransform.mjs', 'scripts/fileDialogTransform.mjs',
  'upstream/src/vs/platform/files/common/fileService.ts', 'upstream/src/vs/workbench/services/dialogs/browser/simpleFileDialog.ts',
  'src/agentApi.ts', 'src/workspaceContext.ts', 'src/workspaceFiles.ts'].map(path => 'src/clients/workbench/' + path),
  'src/clients/web/src/api/http.ts'];
const fingerprint = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path => [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const fingerprints = await fingerprint(), observations = [], browserEvents = [];
const fixture = `
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { URI } from '${upstream}base/common/uri.js';
export { Emitter } from '${upstream}base/common/event.js';
export { SimpleFileDialog } from '${upstream}workbench/services/dialogs/browser/simpleFileDialog.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { FileOperationError, FileOperationResult, toFileOperationResult, toFileSystemProviderErrorCode } from '${upstream}platform/files/common/files.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { OperatingSystem } from '${upstream}base/common/platform.js';
export { installBackendFiles, workspaceFileUri } from './src/clients/workbench/src/workspaceFiles.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
`;
await writeFile(join(run.artifactsDir, 'workspace-file-dialog.ts'), fixture);
const built = await build({ stdin: { contents: fixture, loader: 'ts', resolveDir: resolve('.') }, outfile: bundle,
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' },
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
let browser, server, current;
function envelope(data, requestId, status = 200) {
  return Response.json({ code: 0, data, message: '', request_id: requestId }, { status, headers: { 'X-Request-ID': requestId } });
}
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, async fetch(request) {
    const url = new URL(request.url), path = url.pathname;
    if (path === '/fixture.mjs') return new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } });
    if (!path.startsWith('/api/')) return new Response('<!doctype html><title>工作区文件对话框</title>', { headers: { 'Content-Type': 'text/html' } });
    const state = current, requestId = 'file-dialog-' + (state.requests.length + 1);
    const body = request.method === 'POST' ? await request.json() : undefined;
    const item = { path, query: Object.fromEntries(url.searchParams), method: request.method, body, requestId,
      workspaceId: request.headers.get('X-BoxTeam-Workspace-Id'), gatewayId: request.headers.get('X-BoxTeam-Target-Gateway-Id') };
    state.requests.push(item);
    if (path === '/api/gateway/auth/local-credential') return envelope({ token: 'test-local-credential' }, requestId);
    const fail = status => Response.json({ detail: { code: 'directory-read-failed', message: '原目录读取失败：' + requestId } },
      { status, headers: { 'X-Request-ID': requestId } });
    const emptySegment = () => Response.json({ detail: { owner: 'resource_uri_grammar', code: 'empty_segment', message: 'URI 不得有空 segment' }, request_id: requestId },
      { status: 400, headers: { 'X-Request-ID': requestId } });
    if (path.endsWith('/resources/navigation')) {
      if (body.uri.endsWith('/')) return emptySegment();
      const name = body.uri.split('/').at(-1), gate = state.statGates.get(name);
      if (gate) { gate.seen.resolve(item); await gate.release.promise; }
      if (state.policy.statFailure) return fail(state.policy.statFailure);
      if (state.policy.statFaults?.[name]) return fail(state.policy.statFaults[name]);
      if (name === 'missing-dir' || name === 'new.txt' && !state.policy.existsNewFile) return fail(404);
      return envelope({ node: { uri: body.uri, resource_shape: name.endsWith('.txt') ? 'file' : 'directory',
        stat: { changed_at_ms: 1, modified_at_ms: 2, size: 3 } } }, requestId);
    }
    if (path === '/api/v1/workspace/files') {
      const directory = url.searchParams.get('path');
      if (directory.endsWith('/')) return emptySegment();
      const name = directory.split('/').at(-1), fault = state.policy.directoryFaults?.[name];
      const gate = state.directoryGates.get(name);
      if (gate) { gate.seen.resolve(item); await gate.release.promise; }
      if (typeof fault === 'number') return fail(fault);
      if (fault === 'offline') return fail(503);
      const names = state.policy.empty || name === 'empty' ? [] : name === 'root'
        ? [['packages', 'directory'], ['slow', 'directory'], ['fast', 'directory'], ['empty', 'directory'], ['.hidden.txt', 'file'], ['root.txt', 'file']]
        : [['filename-1.0.0.txt', 'file']];
      return envelope({ path: directory, root_path: directory.split('/resources/files/')[0] + '/resources/files/root',
        directory_key: { workspace_id: item.workspaceId, scope: 'workspace', root: directory.split('/resources/files/')[0] + '/resources/files/root', path: directory },
        directory_revision: 1, items: names.map(([name, kind]) => ({ name, kind, path: directory + '/' + encodeURIComponent(name) })),
        truncated: fault === 'truncated', limit: 500 }, requestId);
    }
    return Response.json({ detail: '未授权的额外请求：' + path }, { status: 500 });
  } });
  run.addCleanup('文件API HTTP边界', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('文件对话框Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    const after = await fingerprint(), drift = sourcePaths.filter(path => fingerprints[path] !== after[path]);
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, fingerprintsAfter: after, drift,
      boundary: '真实 Chromium 运行实际转换的原 FileService、产品 WorkspaceFiles 和原 SimpleFileDialog；Quick Input 控件、路径／语言／上下文／存储服务与 HTTP 目标磁盘为具名边界，不替代真实 Gateway、磁盘错误、完整文件选择器渲染或 Windows 验收。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
    if (drift.length) throw new Error('验证期间源码改变：' + drift.join(', '));
  }
});
async function inBrowser(name, body, policy = {}) {
  const state = { name, policy, requests: [], directoryGates: new Map(), statGates: new Map() };
  current = state;
  const context = await browser.newContext(), page = await context.newPage(), errors = [];
  page.on('pageerror', error => { errors.push(error.message); browserEvents.push({ name, message: error.message }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}/case/${encodeURIComponent(name)}`);
    await page.evaluate(async () => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), lifetime = new m.DisposableStore();
      const target = { gatewayId: 'gateway-a', workspaceId: 'workspace-a1' };
      const root = m.workspaceFileUri(target, 'boxteam://workspace/workspace-a1/resources/files/root', 'boxteam://workspace/workspace-a1/resources/files/root');
      const folders = [{ uri: root, name: 'A1', index: 0 }];
      m.workspaceContext.publish({ gatewayId: target.gatewayId, workspaces: [], activeId: 'active-a1', settings: null, loading: false, error: null });
      const prompts = [], filePicks = [], contextValues = new Map(), storageChanged = lifetime.add(new m.Emitter());
      let hiddenFiles = true;
      const files = lifetime.add(new m.FileService(new m.NullLogService()));
      m.installBackendFiles(files, lifetime.add(new m.UriIdentityService(files)));
      const createFilePick = () => {
        const events = Object.fromEntries(['TriggerButton', 'Custom', 'Accept', 'ChangeActive', 'ChangeValue', 'Hide'].map(name => [name, new m.Emitter()]));
        const picker = { events, visible: false, disposed: false, disposeCalls: 0, lateWrites: 0, writes: [], _value: '', _busy: false, _items: [],
          activeItems: [], selectedItems: [], valueSelection: [0, 0],
          get value() { return this._value; }, set value(value) { if (this.disposed) this.lateWrites++; const changed = this._value !== value; this._value = value; if (changed) events.ChangeValue.fire(value); },
          get busy() { return this._busy; }, set busy(value) { if (this.disposed) this.lateWrites++; this._busy = value; this.writes.push({ busy: value }); },
          get items() { return this._items; }, set items(value) { if (this.disposed) this.lateWrites++; this._items = value; this.writes.push({ items: value.map(item => item.label) }); },
          show() { for (const other of filePicks) if (other !== this) other.hide(); this.visible = true; },
          hide() { if (this.visible) { this.visible = false; events.Hide.fire(); } }, inputHasFocus() { return false; },
          dispose() { this.disposeCalls++; this.hide(); this.disposed = true; for (const event of Object.values(events)) event.dispose(); },
          accept(label) { if (label) { const item = this.items.find(item => item.label === label); if (!item) throw new Error('原对话框没有目录项：' + label);
            this.activeItems = [item]; this.selectedItems = this.canSelectMany && !item.isFolder ? [item] : []; }
            events.Accept.fire(); },
        };
        for (const [name, event] of Object.entries(events)) picker['onDid' + name] = event.event;
        picker.onDidChangeValue = listener => events.ChangeValue.event(value => { picker.inputPromise = listener(value); });
        filePicks.push(picker); return picker;
      };
      const originalDialog = options => {
        const dialog = new m.SimpleFileDialog(files, { createQuickPick: createFilePick }, { getSeparator: () => '/' },
          { getWorkspace: () => ({ folders }) }, { info: message => prompts.push({ severity: 1, message, choices: [] }) },
          { preferredHome: async () => folders[0].uri }, { getModel: () => null }, { guessLanguageIdByFilepathOrFirstLine: () => null },
          {}, { getEnvironment: async () => null },
          { defaultUriScheme: 'boxteam-file', getOperatingSystem: async () => m.OperatingSystem.Linux, getPath: async () => ({ sep: '/' }), userHome: async () => folders[0].uri },
          { lookupKeybinding: () => undefined }, { createKey: (key, value) => { contextValues.set(key, value); return { set: value => contextValues.set(key, value) }; } },
          { isScreenReaderOptimized: () => false }, { getBoolean: () => hiddenFiles, onDidChangeValue: () => storageChanged.event,
            store: (_key, value) => { hiddenFiles = value; storageChanged.fire(); } });
        lifetime.add(dialog);
        const result = options.save ? dialog.showSaveDialog(options) : dialog.showOpenDialog(options);
        result.catch(() => {});
        return { dialog, result };
      };
      const p = { m, filePicks, files, root, completed: false,
        openDialog(options = {}) {
          p.completed = false; p.error = undefined; p.selection = undefined;
          const original = originalDialog({ defaultUri: root, availableFileSystems: ['boxteam-file'], canSelectFiles: true, ...options });
          p.activeDialog = original.dialog;
          p.outcome = original.result.then(value => { p.selection = Array.isArray(value) ? value.map(uri => uri.toString()) : value?.toString(); p.error = null; },
            error => { p.error = { name: error.name, message: error.message, status: error.status, requestId: error.requestId,
              cause: error.cause && { message: error.cause.message, status: error.cause.status, requestId: error.cause.requestId } }; })
            .finally(() => { p.completed = true; });
          return p.outcome;
        },
        fileState() { const picker = filePicks.at(-1); return picker && { value: picker.value, busy: picker.busy, items: picker.items.map(item => item.label),
          visible: picker.visible, disposed: picker.disposed, disposeCalls: picker.disposeCalls, lateWrites: picker.lateWrites,
          currentFolder: p.activeDialog?.currentFolder?.toString(),
          pendingUpdate: !!p.activeDialog?.updatingPromise, context: contextValues.get('remoteFileDialogVisible') }; },
        result() { return { error: p.error, completed: p.completed, selection: p.selection, fileState: p.fileState(), storageListeners: storageChanged.hasListeners() }; },
        dispose() { lifetime.dispose(); },
      };
      globalThis.fileDialog = p;
    });
    await body(page, state);
    expect(errors).toEqual([]);
    expect(state.requests.filter(item => item.path !== '/api/gateway/auth/local-credential'
      && item.path !== '/api/v1/workspace/files' && !item.path.endsWith('/resources/navigation'))).toEqual([]);
    expect(state.requests.filter(item => item.path === '/api/v1/workspace/files' || item.path.endsWith('/resources/navigation'))
      .filter(item => (item.body?.uri ?? item.query.path).endsWith('/'))).toEqual([]);
  } finally {
    for (const gate of state.directoryGates.values()) gate.release.resolve();
    for (const gate of state.statGates.values()) gate.release.resolve();
    observations.push({ name, requests: state.requests, outcome: await page.evaluate(() => globalThis.fileDialog?.result()).catch(() => undefined), pageErrors: errors });
    await page.evaluate(() => globalThis.fileDialog?.dispose()); await context.close();
  }
}
const invoke = page => page.evaluate(async () => { const p = globalThis.fileDialog; await p.openDialog(); return p.result(); });
const beginPicker = page => page.evaluate(() => { void globalThis.fileDialog.openDialog(); });
const pickerReady = page => page.waitForFunction(() => { const p = globalThis.fileDialog, state = p.fileState(); return state && !state.busy && !state.disposed && !p.completed; });
const pickerCompleted = page => page.waitForFunction(() => globalThis.fileDialog.completed);
const listRequests = state => state.requests.filter(item => item.path === '/api/v1/workspace/files');
const hold = () => ({ seen: Promise.withResolvers(), release: Promise.withResolvers() });

for (const fault of [403, 404, 503, 'truncated']) {
  test(`真实原文件对话框目录${fault}拒绝原对话框，不返回空目录`, async () => inBrowser('original-directory-' + fault,
    async (page, state) => {
      const result = await invoke(page);
      expect(result.error.message).toContain(fault === 'truncated' ? '分页游标' : '原目录读取失败：');
      if (typeof fault === 'number') expect((result.error.cause ?? result.error).status).toBe(fault);
      expect(result.completed).toBe(true);
     expect(listRequests(state)).toHaveLength(1);
     expect(listRequests(state)[0]).toMatchObject({ gatewayId: 'gateway-a', workspaceId: 'workspace-a1', query: { scope: 'workspace' } });
      expect(result.fileState).toMatchObject({ busy: false, visible: false, disposed: true, disposeCalls: 1, pendingUpdate: false, context: false, lateWrites: 0 });
    }, { directoryFaults: { root: fault } }));
}

test('原FileService区分stat缺失候选和readdir404，两者保留准确原cause', async () => inBrowser('original-resolve-phases',
  async (page, state) => {
    const resolve = page => page.evaluate(async () => {
      const p = globalThis.fileDialog, m = p.m;
      try { await p.files.resolve(p.root); throw new Error('应拒绝失败目录'); }
      catch (error) { return { lookup: error instanceof m.FileOperationError, code: m.toFileSystemProviderErrorCode(error),
        operation: m.toFileOperationResult(error), message: error.message, cause: { status: error.cause?.status, requestId: error.cause?.requestId } }; }
    });
    const directory = await resolve(page);
    expect(directory).toMatchObject({ lookup: false, code: 'EntryNotFound', operation: 1, message: expect.stringContaining('原目录读取失败：'),
      cause: { status: 404, requestId: listRequests(state)[0].requestId } });
    const missing = await page.evaluate(async () => {
      const p = globalThis.fileDialog, m = p.m;
      try { await p.files.resolve(m.URI.joinPath(p.root, 'new.txt')); throw new Error('应拒绝缺失候选'); }
      catch (error) { return { lookup: error instanceof m.FileOperationError, operation: m.toFileOperationResult(error), message: error.message,
        cause: { code: m.toFileSystemProviderErrorCode(error.cause), status: error.cause?.cause?.status, requestId: error.cause?.cause?.requestId } }; }
    });
    expect(missing).toMatchObject({ lookup: true, operation: 1, message: expect.stringContaining('原目录读取失败：'),
      cause: { code: 'EntryNotFound', status: 404, requestId: state.requests.at(-1).requestId } });

  }, { directoryFaults: { root: 404 } }));

for (const recursive of [true, false]) {
  test(`原FileService${recursive ? '递归目录' : '子项metadata'}拒绝不得丢项伪装成功`, async () => inBrowser('original-resolve-child-' + recursive,
    async (page, state) => {
      const result = await page.evaluate(async recursive => {
        const p = globalThis.fileDialog;
        try { await p.files.resolve(p.root, recursive ? { resolveTo: [p.m.URI.joinPath(p.root, 'packages')] } : { resolveMetadata: true }); return { success: true }; }
        catch (error) { return { message: error.message, code: p.m.toFileSystemProviderErrorCode(error), status: error.cause?.status }; }
      }, recursive);
      expect(result).toMatchObject({ message: expect.stringContaining('原目录读取失败：'), code: 'NoPermissions', status: 403 });

    }, recursive ? { directoryFaults: { packages: 403 } } : { statFaults: { 'root.txt': 403 } }));
}

test('真实stat权限错误在创建picker前保留cause，不能探测其它目录', async () => inBrowser('original-stat-denied',
  async (page, state) => {
    const result = await invoke(page);
    expect(result.error).toMatchObject({ message: expect.stringContaining('原目录读取失败：'), cause: { status: 403 } });
    expect(result.fileState).toBeUndefined(); expect(listRequests(state)).toEqual([]);
    expect(result.storageListeners).toBe(false);
    expect(state.requests.filter(item => item.path.endsWith('/resources/navigation'))).toHaveLength(1);
  }, { statFailure: 403 }));

test('folder-only祖先检查的原权限拒绝不能误报不存在', async () => inBrowser('original-folder-ancestor-denied',
  async (page, state) => {
    await page.evaluate(() => { void globalThis.fileDialog.openDialog({ canSelectFiles: false, canSelectFolders: true }); }); await pickerReady(page);
    state.policy.statFaults = { root: 403 };
    const result = await page.evaluate(async () => {
      const p = globalThis.fileDialog;
      try { return { value: await p.activeDialog.validate(p.m.URI.joinPath(p.root, 'missing-dir', 'new.txt')) }; }
      catch (error) { return { message: error.message, status: error.cause?.status, requestId: error.cause?.requestId }; }
    });
    expect(result).toMatchObject({ message: expect.stringContaining('原目录读取失败：'), status: 403, requestId: state.requests.at(-1).requestId });
    expect(state.requests.at(-1).body.uri).toBe('boxteam://workspace/workspace-a1/resources/files/root');

    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
  }));

test('合法空目录保持成功等待，用户取消一次结算并清理原picker', async () => inBrowser('original-empty',
  async (page, state) => {
    await beginPicker(page); await pickerReady(page);
    expect(await page.evaluate(() => globalThis.fileDialog.fileState())).toMatchObject({ items: [], busy: false, disposed: false, context: true });
    expect(listRequests(state)).toHaveLength(1);
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
    const result = await page.evaluate(() => globalThis.fileDialog.result());
    expect(result.error).toBeNull();
    expect(result.fileState).toMatchObject({ disposed: true, disposeCalls: 1, pendingUpdate: false, context: false, lateWrites: 0 });
  }, { empty: true }));

test('原dialog目录尾分隔符只留在输入框，真实HTTP stat与list均无尾空段', async () => inBrowser('original-directory-separator',
  async (page, state) => {
    await beginPicker(page); await pickerReady(page);
    await page.evaluate(async () => { const p = globalThis.fileDialog, picker = p.filePicks.at(-1); picker.value = p.root.path + '/packages/'; await picker.inputPromise; });
    const current = await page.evaluate(() => globalThis.fileDialog.fileState());
    expect(current.value).toEndWith('/packages/'); expect(current.currentFolder).toEndWith('/packages/');
    expect(current).toMatchObject({ busy: false, visible: true, disposed: false });
    expect(listRequests(state).map(item => item.query.path)).toEqual([
      'boxteam://workspace/workspace-a1/resources/files/root', 'boxteam://workspace/workspace-a1/resources/files/root/packages',
    ]);
    expect(state.requests.filter(item => item.path.endsWith('/resources/navigation') && item.body.uri.endsWith('/packages')).map(item => item.body.uri))
      .toEqual(['boxteam://workspace/workspace-a1/resources/files/root/packages', 'boxteam://workspace/workspace-a1/resources/files/root/packages']);
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
    expect((await page.evaluate(() => globalThis.fileDialog.result())).error).toBeNull();
  }));

test('目录拒绝先终结，再解除busy；排队接受不能使用旧列表', async () => inBrowser('original-queued-accept',
  async (page, state) => {
    const gate = hold(); state.directoryGates.set('root', gate);
    await beginPicker(page); await gate.seen.promise;
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).accept());
    expect(await page.evaluate(() => globalThis.fileDialog.completed)).toBe(false);
    gate.release.resolve(); await pickerCompleted(page);
    const result = await page.evaluate(() => globalThis.fileDialog.result());
    expect(result.error.message).toContain('原目录读取失败：'); expect(result.fileState.busy).toBe(false);
  }, { directoryFaults: { root: 403 } }));

for (const entry of ['accept', 'input', 'hidden']) {
  test(`原${entry}入口的目录拒绝接回同一对话框Promise`, async () => inBrowser('original-entry-' + entry,
    async (page, state) => {
      await beginPicker(page); await pickerReady(page);
      state.policy.directoryFaults = { root: 403, packages: 403 };
      await page.evaluate(entry => {
        const p = globalThis.fileDialog, picker = p.filePicks.at(-1);
        if (entry === 'accept') picker.accept('packages');
        else if (entry === 'hidden') picker.events.TriggerButton.fire({});
        else picker.value = p.root.path + '/packages/';
      }, entry);
      await pickerCompleted(page);
      const result = await page.evaluate(() => globalThis.fileDialog.result());
      expect(result.error.message).toContain('原目录读取失败：');
     expect(listRequests(state)).toHaveLength(2);
      expect(result.fileState).toMatchObject({ busy: false, disposed: true, disposeCalls: 1, pendingUpdate: false, context: false, lateWrites: 0 });
    }));
}

test('原对话框失败后明确重开恢复，仍返回原工作区文件URI', async () => inBrowser('original-retry',
  async (page, state) => {
    expect((await invoke(page)).error.message).toContain('原目录读取失败：');
     state.policy.directoryFaults = {};
    await beginPicker(page); await pickerReady(page);
    await page.evaluate(() => { const p = globalThis.fileDialog; p.filePicks.at(-1).accept('packages'); });
    await page.waitForFunction(() => globalThis.fileDialog.fileState()?.items.includes('filename-1.0.0.txt') && !globalThis.fileDialog.fileState().busy);
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).accept('filename-1.0.0.txt')); await pickerCompleted(page);
    const result = await page.evaluate(() => globalThis.fileDialog.result());
    expect(result.error).toBeNull(); expect(result.selection).toEqual([await page.evaluate(() => globalThis.fileDialog.m.URI.joinPath(globalThis.fileDialog.root, 'packages', 'filename-1.0.0.txt').toString())]);
    expect(result.fileState).toMatchObject({ disposed: true, disposeCalls: 1, pendingUpdate: false, context: false });
  }, { directoryFaults: { root: 403 } }));

for (const oldFault of [undefined, 403]) {
  test(`旧目录请求取消后迟到${oldFault ?? '成功'}不清除新busy或覆盖items`, async () => inBrowser('original-stale-' + oldFault,
    async (page, state) => {
      await beginPicker(page); await pickerReady(page);
      const slow = hold(), fast = hold(); state.directoryGates.set('slow', slow); state.directoryGates.set('fast', fast);
      state.policy.directoryFaults = { slow: oldFault };
      await page.evaluate(() => { const p = globalThis.fileDialog; p.filePicks.at(-1).value = p.root.path + '/slow/'; }); await slow.seen.promise;
      await page.evaluate(() => { const p = globalThis.fileDialog; p.filePicks.at(-1).value = p.root.path + '/fast/'; }); await fast.seen.promise;
      const late = page.waitForResponse(response => new URL(response.url()).searchParams.get('path')?.replace(/\/$/, '').endsWith('/slow'));
      slow.release.resolve(); await (await late).finished();
      const waiting = await page.evaluate(() => ({ state: globalThis.fileDialog.fileState(), completed: globalThis.fileDialog.completed }));
      expect(waiting.completed).toBe(false); expect(waiting.state).toMatchObject({ busy: true, pendingUpdate: true, disposed: false });
      expect(waiting.state.value).toEndWith('/fast/'); expect(waiting.state.items).toContain('packages');
      fast.release.resolve(); await pickerReady(page);
      expect(await page.evaluate(() => globalThis.fileDialog.fileState())).toMatchObject({ items: ['..', 'filename-1.0.0.txt'], busy: false, pendingUpdate: false });
      await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
      expect((await page.evaluate(() => globalThis.fileDialog.result())).error).toBeNull();
    }));
}

for (const parentProbe of [false, true]) for (const lateFault of [undefined, 403]) {
test(`旧输入${parentProbe ? '父目录' : '目录'}stat迟到${lateFault ?? '成功'}不能重启旧读取或报错`, async () => inBrowser('original-stale-stat-' + parentProbe + '-' + lateFault,
  async (page, state) => {
    await beginPicker(page); await pickerReady(page);
    const gate = hold(); state.statGates.set('slow', gate); state.policy.statFaults = { slow: lateFault };
    await page.evaluate(parentProbe => { const p = globalThis.fileDialog, picker = p.filePicks.at(-1);
      picker.value = p.root.path + (parentProbe ? '/slow/new.txt' : '/slow/'); p.slowInput = picker.inputPromise; }, parentProbe);
    await gate.seen.promise;
    await page.evaluate(async () => { const p = globalThis.fileDialog, picker = p.filePicks.at(-1); picker.value = p.root.path + '/fast/'; await picker.inputPromise; });
    expect((await page.evaluate(() => globalThis.fileDialog.fileState())).currentFolder).toEndWith('/fast/');
    gate.release.resolve(); await page.evaluate(() => globalThis.fileDialog.slowInput);
    const current = await page.evaluate(() => globalThis.fileDialog.fileState());
    expect(current.currentFolder).toEndWith('/fast/'); expect(current.value).toEndWith('/fast/');
    expect(await page.evaluate(() => globalThis.fileDialog.completed)).toBe(false);
    expect(listRequests(state).map(item => item.query.path.replace(/\/$/, '').split('/').at(-1))).toEqual(['root', 'fast']);

    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
  }));
}

test('加载期间取消立即结算，迟到失败不更新已dispose控件', async () => inBrowser('original-cancel-pending',
  async (page, state) => {
    const gate = hold(); state.directoryGates.set('root', gate);
    await beginPicker(page); await gate.seen.promise;
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
    const before = await page.evaluate(() => globalThis.fileDialog.fileState());
    const late = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/workspace/files');
    gate.release.resolve(); await (await late).finished();
    expect(await page.evaluate(() => globalThis.fileDialog.fileState())).toEqual(before);
    expect(before).toMatchObject({ busy: false, disposed: true, disposeCalls: 1, pendingUpdate: false, lateWrites: 0, context: false });
    expect((await page.evaluate(() => globalThis.fileDialog.result())).error).toBeNull();
  }, { directoryFaults: { root: 403 } }));

for (const missingParent of [false, true]) {
  test(`保存新文件${missingParent ? '及缺失父目录确认' : '候选'}保留原返回URI合同`, async () => inBrowser('original-save-' + missingParent,
    async (page, state) => {
      await page.evaluate(missingParent => { const p = globalThis.fileDialog; void p.openDialog({ save: true,
        defaultUri: p.m.URI.joinPath(p.root, ...(missingParent ? ['missing-dir', 'new.txt'] : ['new.txt'])) }); }, missingParent);
      await pickerReady(page); await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).accept());
      if (missingParent) {
        await page.waitForFunction(() => globalThis.fileDialog.filePicks.length === 2 && globalThis.fileDialog.filePicks.at(-1).visible);
        expect(await page.evaluate(() => globalThis.fileDialog.completed)).toBe(false);
        await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).accept());
      }
      await pickerCompleted(page); const result = await page.evaluate(() => globalThis.fileDialog.result());
      expect(result.error).toBeNull(); expect(result.selection).toEndWith(missingParent ? '/missing-dir/new.txt' : '/new.txt');

      expect(await page.evaluate(() => globalThis.fileDialog.filePicks.map(picker => ({ disposed: picker.disposed, lateWrites: picker.lateWrites })))).toEqual(
        Array.from({ length: missingParent ? 2 : 1 }, () => ({ disposed: true, lateWrites: 0 })));
    }));
}

test('关闭后接受验证的迟到stat不会创建确认prompt或返回选择', async () => inBrowser('original-cancel-validating',
  async (page, state) => {
    await page.evaluate(() => { const p = globalThis.fileDialog; void p.openDialog({ save: true, defaultUri: p.m.URI.joinPath(p.root, 'new.txt') }); });
    await pickerReady(page); state.policy.existsNewFile = true;
    const gate = hold(); state.statGates.set('new.txt', gate);
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).accept()); await gate.seen.promise;
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
    const late = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/resources/navigation') && response.request().postDataJSON().uri.endsWith('/new.txt'));
    gate.release.resolve(); await (await late).finished();
    const result = await page.evaluate(() => ({ result: globalThis.fileDialog.result(), picks: globalThis.fileDialog.filePicks.length }));
    expect(result.picks).toBe(1); expect(result.result.error).toBeNull(); expect(result.result.selection).toBeUndefined();
    expect(result.result.fileState).toMatchObject({ disposed: true, pendingUpdate: false, lateWrites: 0 });
  }));

test('确认prompt期间dispose结清原validate和picker，不能复活原控件', async () => inBrowser('original-dispose-confirmation',
  async (page, state) => {
    await page.evaluate(() => { const p = globalThis.fileDialog; void p.openDialog({ save: true, defaultUri: p.m.URI.joinPath(p.root, 'new.txt') }); });
    await pickerReady(page); state.policy.existsNewFile = true;
    await page.evaluate(() => { const p = globalThis.fileDialog; p.validation = p.activeDialog.validate(p.m.URI.joinPath(p.root, 'new.txt'))
      .then(value => { p.validationValue = value; p.validationCompleted = true; }); });
    await page.waitForFunction(() => globalThis.fileDialog.filePicks.length === 2 && globalThis.fileDialog.filePicks.at(-1).visible);
    await page.evaluate(() => globalThis.fileDialog.activeDialog.dispose()); await pickerCompleted(page);
    await page.waitForFunction(() => globalThis.fileDialog.validationCompleted);
    expect(await page.evaluate(() => ({ value: globalThis.fileDialog.validationValue, selection: globalThis.fileDialog.selection,
      picks: globalThis.fileDialog.filePicks.map(picker => ({ disposed: picker.disposed, visible: picker.visible, lateWrites: picker.lateWrites })) })))
      .toEqual({ value: false, selection: undefined, picks: [{ disposed: true, visible: false, lateWrites: 0 }, { disposed: true, visible: false, lateWrites: 0 }] });

  }));

for (const denied of [false, true]) {
test(`非工作区scheme${denied ? '拒绝仍按原宽容合同' : '正常空目录'}沿原picker取消`, async () => inBrowser('original-native-contract-' + denied,
  async (page, state) => {
    const result = await page.evaluate(async denied => {
      const p = globalThis.fileDialog, m = p.m;
      const provider = denied ? new class extends m.InMemoryFileSystemProvider { async readdir() { throw new Error('原native目录失败'); } }()
        : new m.InMemoryFileSystemProvider();
      const registration = p.files.registerProvider('file', provider);
      const stat = await p.files.resolve(m.URI.file('/'));
      p.nativeRegistration = registration; p.nativeProvider = provider;
      void p.openDialog({ defaultUri: m.URI.file('/'), availableFileSystems: ['file'] });
      return { isDirectory: stat.isDirectory, children: stat.children };
    }, denied);
    expect(result).toEqual({ isDirectory: true, children: [] }); await pickerReady(page);
    expect((await page.evaluate(() => globalThis.fileDialog.fileState())).items).toEqual([]);
    await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).hide()); await pickerCompleted(page);
    const canceled = await page.evaluate(() => { const p = globalThis.fileDialog, result = p.result(); p.nativeRegistration.dispose(); p.nativeProvider.dispose(); return result; });
    expect(canceled.error).toBeNull(); expect(canceled.selection).toBeUndefined();
    expect(canceled.fileState).toMatchObject({ busy: false, disposed: true, lateWrites: 0, context: false });
    expect(listRequests(state)).toEqual([]);
  }));
}

test('非工作区scheme原picker成功选择仍返回原file URI', async () => inBrowser('original-native-success',
  async (page, state) => {
    await page.evaluate(async () => {
      const p = globalThis.fileDialog, m = p.m, provider = new m.InMemoryFileSystemProvider();
      const registration = p.files.registerProvider('file', provider);
      await provider.writeFile(m.URI.file('/native.txt'), new Uint8Array([1]), { create: true, overwrite: false });
      p.nativeRegistration = registration; p.nativeProvider = provider;
      void p.openDialog({ defaultUri: m.URI.file('/'), availableFileSystems: ['file'] });
    });
    await pickerReady(page); await page.evaluate(() => globalThis.fileDialog.filePicks.at(-1).accept('native.txt')); await pickerCompleted(page);
    const result = await page.evaluate(() => { const p = globalThis.fileDialog, result = p.result(); p.nativeRegistration.dispose(); p.nativeProvider.dispose(); return result; });
    expect(result.error).toBeNull(); expect(result.selection).toEqual(['file:///native.txt']);
    expect(result.fileState).toMatchObject({ busy: false, disposed: true, disposeCalls: 1, lateWrites: 0, context: false });
    expect(listRequests(state)).toEqual([]);
  }));
