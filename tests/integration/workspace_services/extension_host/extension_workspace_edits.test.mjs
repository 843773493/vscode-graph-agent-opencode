import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_workspace_edits.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'workspace-edits.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/', transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const apiPath = join(root, 'upstream/src/vs/workbench/api/common/extHost.api.impl.ts');
const parsed = ts.createSourceFile(apiPath, await transform(apiPath, await readFile(apiPath, 'utf8')), ts.ScriptTarget.Latest, true);
const methods = [];
function collectApplyEdit(node) {
  if (ts.isMethodDeclaration(node) && node.name.getText(parsed) === 'applyEdit'
    && node.body?.getText(parsed).includes('extHostBulkEdits.applyWorkspaceEdit')) methods.push(node);
  ts.forEachChild(node, collectApplyEdit);
}
collectApplyEdit(parsed);
if (methods.length !== 1) throw new Error('原 workspace.applyEdit 入口数量已变化：' + methods.length);
const applyEditMethod = methods[0].getText(parsed);
await writeFile(join(run.artifactsDir, 'original-workspace-apply-edit.ts'), applyEditMethod + '\n');
const built = await build({ stdin: { loader: 'ts', resolveDir: resolve('.'), contents: `
export function originalWorkspaceApi(extHostBulkEdits, extension) { return { ${applyEditMethod} }; }
export { MainThreadBulkEdits } from '${upstream}workbench/api/browser/mainThreadBulkEdits.js';
export { ExtHostBulkEdits } from '${upstream}workbench/api/common/extHostBulkEdits.js';
export { WorkspaceEdit } from '${upstream}workbench/api/common/extHostTypes/workspaceEdit.js';
export { Range } from '${upstream}workbench/api/common/extHostTypes/range.js';
export { Position } from '${upstream}workbench/api/common/extHostTypes/position.js';
export { BulkEditService } from '${upstream}workbench/contrib/bulkEdit/browser/bulkEditService.js';
export { ResourceTextEdit, ResourceFileEdit } from '${upstream}editor/browser/services/bulkEditService.js';
export { ModelService } from '${upstream}editor/common/services/modelService.js';
export { IModelService } from '${upstream}editor/common/services/model.js';
export { ITextModelService } from '${upstream}editor/common/services/resolverService.js';
export { IEditorWorkerService } from '${upstream}editor/common/services/editorWorker.js';
export { ITextResourcePropertiesService } from '${upstream}editor/common/services/textResourceConfiguration.js';
export { LanguageService } from '${upstream}editor/common/services/languageService.js';
export { ILanguageService } from '${upstream}editor/common/languages/language.js';
export { ILanguageConfigurationService, LanguageConfigurationService } from '${upstream}editor/common/languages/languageConfigurationRegistry.js';
export { ITreeSitterLibraryService } from '${upstream}editor/common/services/treeSitter/treeSitterLibraryService.js';
export { UndoRedoService } from '${upstream}platform/undoRedo/common/undoRedoService.js';
export { IUndoRedoService } from '${upstream}platform/undoRedo/common/undoRedo.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { IFileService, FileOperation } from '${upstream}platform/files/common/files.js';
export { WorkingCopyFileService, IWorkingCopyFileService } from '${upstream}workbench/services/workingCopy/common/workingCopyFileService.js';
export { WorkingCopyService, IWorkingCopyService } from '${upstream}workbench/services/workingCopy/common/workingCopyService.js';
export { ITextFileService } from '${upstream}workbench/services/textfile/common/textfiles.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { IUriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentity.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { IConfigurationService } from '${upstream}platform/configuration/common/configuration.js';
export { IProgressService } from '${upstream}platform/progress/common/progress.js';
export { IDialogService } from '${upstream}platform/dialogs/common/dialogs.js';
export { INotificationService } from '${upstream}platform/notification/common/notification.js';
export { IEditorService } from '${upstream}workbench/services/editor/common/editorService.js';
export { ILifecycleService } from '${upstream}workbench/services/lifecycle/common/lifecycle.js';
export { ILogService, NullLogService } from '${upstream}platform/log/common/log.js';
export { MainContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { SerializableObjectWithBuffers } from '${upstream}workbench/services/extensions/common/proxyIdentifier.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { URI } from '${upstream}base/common/uri.js';
export { dirname } from '${upstream}base/common/resources.js';
export { VSBuffer, encodeBase64 } from '${upstream}base/common/buffer.js';
export { TargetUriTransformer } from './src/clients/workbench/src/extensions/targetUris.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' }, plugins: [buildSourcePlugin(root, transform)],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const sources = ['scripts/sourceTransform.mjs', 'scripts/extensions/extensionFileEventsTransform.mjs', 'src/extensions/targetUris.ts',
  'upstream/src/vs/workbench/api/common/extHost.api.impl.ts', 'upstream/src/vs/workbench/api/common/extHostBulkEdits.ts',
  'upstream/src/vs/workbench/api/common/extHostTypeConverters.ts', 'upstream/src/vs/workbench/api/common/extHostTypes/workspaceEdit.ts',
  'upstream/src/vs/workbench/api/browser/mainThreadBulkEdits.ts', 'upstream/src/vs/workbench/contrib/bulkEdit/browser/bulkEditService.ts',
  'upstream/src/vs/workbench/contrib/bulkEdit/browser/bulkTextEdits.ts', 'upstream/src/vs/workbench/contrib/bulkEdit/browser/bulkFileEdits.ts',
  'upstream/src/vs/editor/common/model/textModel.ts', 'upstream/src/vs/editor/common/services/modelService.ts',
  'upstream/src/vs/workbench/services/workingCopy/common/workingCopyFileService.ts'];
const fingerprints = Object.fromEntries(await Promise.all(sources.map(async path => [path, createHash('sha256').update(await readFile(join(root, path))).digest('hex')])));
const checks = [], observations = [], browserEvents = [];
let browser, server;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs' ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>工作区编辑组合验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('编辑组件HTTP来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('编辑组件Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, checks,
      boundary: '真实 Chromium 中 AST 提取的原 workspace.applyEdit、ExtHostBulkEdits/转换器、RPCProtocol/SerializableObjectWithBuffers/TargetUriTransformer、MainThreadBulkEdits、BulkEditService、BulkTextEdits/ModelService/TextModel/UndoRedoService 和 BulkFileEdits/WorkingCopyFileService/FileService。认证 binding、字节输送、文档版本镜像、已加载模型 resolver、配置/UI 与纯文本 TreeSitter 库是具名装具；目标 WorkspaceFiles 和配置文件 provider 明确由原 InMemoryFileSystemProvider 替代。具名配置仅验证原资源映射和已加载模型编辑，不代表真实配置保存、产品 WorkspaceFiles 的 rename/delete 支持、Gateway、真实 Node、磁盘保存、Notebook、Windows、整批版本原子性或所有取消时点。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
  }
});

async function inBrowser(name, body) {
  const context = await browser.newContext(), page = await context.newPage(), errors = [];
  page.on('pageerror', error => { errors.push(error.message); browserEvents.push({ name, message: error.message }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}`);
    await page.evaluate(async () => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), lifetime = new m.DisposableStore(), clients = [], warnings = [], failures = [], resolutions = [], operations = [];
      class RecordingLog extends m.NullLogService {
        warn(...args) { warnings.push(args.map(String).join(' ')); }
        error(...args) { failures.push(args.map(String).join(' ')); }
      }
      const unexpected = label => () => { throw new Error('本场景不应调用外部服务：' + label); };
      const log = new RecordingLog(), files = lifetime.add(new m.FileService(log));
      // 真实内存 provider 持有全部文件；没有目标磁盘、HTTP 或 Gateway 写入替代实现。
      for (const scheme of ['boxteam-file', 'boxteam', 'file']) {
        const provider = lifetime.add(new m.InMemoryFileSystemProvider()); lifetime.add(files.registerProvider(scheme, provider));
      }
      const identity = lifetime.add(new m.UriIdentityService(files)), workingCopies = lifetime.add(new m.WorkingCopyService());
      const config = { onDidChangeConfiguration: m.Event.None, getValue(key) {
        if (key === 'editor') return { detectIndentation: false };
        if (key === 'files.participants.timeout') return 1000;
        if (['files.refactoring.autoSave', 'files.enableTrash', 'files.restoreUndoStack'].includes(key)) return false;
        return undefined;
      } };
      const dialog = { confirm: unexpected('窗口确认') }, notification = { error: unexpected('窗口错误通知'), warn: unexpected('窗口警告通知') };
      const undoRedo = new m.UndoRedoService(dialog, notification), language = lifetime.add(new m.LanguageService());
      const services = new m.ServiceCollection([m.ILogService, log], [m.IFileService, files], [m.IUriIdentityService, identity],
        [m.IWorkingCopyService, workingCopies], [m.IConfigurationService, config], [m.IUndoRedoService, undoRedo],
        [m.ILanguageService, language], [m.IDialogService, dialog], [m.INotificationService, notification],
        [m.IEditorService, { activeTextEditorControl: undefined }], [m.ILifecycleService, { onBeforeShutdown: m.Event.None }],
        [m.IProgressService, { withProgress: unexpected('保存进度UI') }],
        [m.ITextFileService, { getEncodedReadable: unexpected('省略内容的文件编码') }],
        [m.ITextResourcePropertiesService, { getEOL: () => '\n' }],
        [m.ITreeSitterLibraryService, { supportsLanguage(languageId) {
          if (languageId !== 'plaintext') throw new Error('本装具只提供纯文本模型'); return false;
        } }], [m.IEditorWorkerService, { computeMoreMinimalEdits: unexpected('活动编辑器最小化编辑') }]);
      const insta = lifetime.add(new m.InstantiationService(services, true));
      const languageConfig = lifetime.add(insta.createInstance(m.LanguageConfigurationService)); services.set(m.ILanguageConfigurationService, languageConfig);
      const models = lifetime.add(insta.createInstance(m.ModelService)); services.set(m.IModelService, models);
      // resolver 仅取得原 ModelService 已有的真实 TextModel；不实现编辑算法或文件写入。
      services.set(m.ITextModelService, { async createModelReference(resource) {
        const model = models.getModel(resource);
        if (!model) throw new Error('装具没有载入该模型：' + resource.toString());
        const observation = { resource: resource.toString(), disposed: false }; resolutions.push(observation);
        return { object: { textEditorModel: model }, dispose() { observation.disposed = true; } };
      } });
      const workingFiles = lifetime.add(insta.createInstance(m.WorkingCopyFileService)); services.set(m.IWorkingCopyFileService, workingFiles);
      lifetime.add(workingFiles.onWillRunWorkingCopyFileOperation(event => operations.push({ phase: 'will', operation: event.operation,
        files: event.files.map(file => ({ source: file.source?.toString(), target: file.target.toString() })) })));
      lifetime.add(workingFiles.onDidRunWorkingCopyFileOperation(event => operations.push({ phase: 'did', operation: event.operation,
        files: event.files.map(file => ({ source: file.source?.toString(), target: file.target.toString() })) })));
      function target(label) {
        const roots = label === 'A' ? ['a1', 'a2'] : ['b1'];
        const binding = { gateway_id: 'gateway-edits', backend_id: 'backend-' + label, initialization: { os: 'linux',
          logs_location_uri: `file:///hosts/${label}/logs`, environment: { app_root_uri: `file:///hosts/${label}/app`,
            global_storage_home_uri: `file:///hosts/${label}/global`, workspace_storage_home_uri: `file:///hosts/${label}/storage`, profile_id: 'default',
            remote_settings: { native_uri: `file:///hosts/${label}/profile/settings.json`, resource_uri: `boxteam://gateway-edits/config/${label}/profile/settings` } },
          workspace: { id: 'view-' + label, configuration: { native_uri: `file:///hosts/${label}/view/workspace.code-workspace`,
            resource_uri: `boxteam://gateway-edits/config/${label}/view/workspace` }, folders: roots.map(workspace_id => ({ workspace_id,
            name: workspace_id, native_uri: `file:///targets/${workspace_id}/project`, resource_uri: `boxteam://workspace/${workspace_id}/resources/files/root` })) } } };
        return { binding, uriTransformer: new m.TargetUriTransformer(binding, () => []), runningLocation: { kind: 3 } };
      }
      async function connect(host) {
        const calls = [], documents = new Map(), toMain = new m.Emitter(), toHost = new m.Emitter();
        const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)), drain: async () => {} }, null, host.uriTransformer);
        const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)), drain: async () => {} });
        const actualBulk = insta.createInstance(m.BulkEditService);
        // 只观察准入边界并原样转交；所有结果、资源统计、版本检查和写入都由原 BulkEditService 决定。
        const observedBulk = { apply(edits, options) { calls.push({ edits: edits.edits, options }); return actualBulk.apply(edits, options); } };
        const main = new m.MainThreadBulkEdits({ extensionHost: host }, observedBulk, log, identity);
        mainRpc.set(m.MainContext.MainThreadBulkEdits, main);
        const extHost = new m.ExtHostBulkEdits({ getProxy: hostRpc.getProxy.bind(hostRpc) }, { getDocument: uri => documents.get(uri.toString()) });
        const workspace = m.originalWorkspaceApi(extHost, { identifier: { value: 'probe.workspace-edits' } });
        const client = { host, calls, documents, main, actualBulk, workspace, remote: hostRpc.getProxy(m.MainContext.MainThreadBulkEdits),
          logical: uri => host.uriTransformer ? m.URI.revive(host.uriTransformer.transformIncoming(uri)) : uri,
          close() { main.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose(); } };
        clients.push(client);
        for (const folder of host.uriTransformer?.folders ?? []) await files.createFolder(folder.uri);
        return client;
      }
      function openText(client, native, value) {
        const model = models.createModel(value, null, client.logical(native));
        client.documents.set(native.toString(), { version: model.getVersionId() }); return model;
      }
      const native = (client, rootIndex, name) => m.URI.joinPath(client.host.uriTransformer.folders[rootIndex].native, name);
      const seed = async (resource, value) => { await files.createFolder(m.dirname(resource)); await files.createFile(resource, m.VSBuffer.fromString(value)); };
      const read = async resource => (await files.readFile(resource)).value.toString();
      const dtoText = (resource, text = 'changed') => ({ resource, textEdit: { range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }, text } });
      const dtoCreate = (newResource, text = 'created') => ({ newResource, options: { contents: { type: 'base64', value: m.encodeBase64(m.VSBuffer.fromString(text)) } } });
      const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
      globalThis.editProbe = { m, target, connect, openText, native, seed, read, dtoText, dtoCreate, defer, files, models, workingFiles,
        warnings, failures, resolutions, operations, lifetime,
        dispose() { for (const client of clients.reverse()) client.close(); for (const model of models.getModels()) model.dispose(); lifetime.dispose(); } };
    });
    const result = await page.evaluate(body); expect(errors).toEqual([]); observations.push({ name, result }); return result;
  } finally {
    try { await page.evaluate(() => globalThis.editProbe?.dispose()); }
    finally { await context.close(); }
  }
}

test('原 workspace.applyEdit 经真实 RPC 修改双根 TextModel，并在原内存 provider 完成 create/rename/delete', async () => {
  const result = await inBrowser('原公开API、双根文本和真实内存文件副作用', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const one = p.native(a, 0, 'one.ts'), two = p.native(a, 1, 'two.ts'), peer = p.native(b, 0, 'peer.ts');
    const first = p.openText(a, one, 'old-one'), second = p.openText(a, two, 'old-two'), untouched = p.openText(b, peer, 'peer-original');
    const created = p.native(a, 0, 'created.ts'), before = p.native(a, 0, 'rename.ts'), after = p.native(a, 1, 'renamed.ts'), deleted = p.native(a, 1, 'delete.ts');
    await p.seed(a.logical(before), 'rename-content'); await p.seed(a.logical(deleted), 'delete-content');
    const previews = [];
    p.lifetime.add(a.actualBulk.setPreviewHandler(async (edits, options) => {
      previews.push({ types: edits.map(edit => edit instanceof m.ResourceTextEdit ? 'text' : edit instanceof m.ResourceFileEdit ? 'file' : 'other'),
        contents: (await edits.find(edit => edit.newResource?.toString() === a.logical(created).toString()).options.contents).toString(),
        typed: edits.every(edit => [edit.resource, edit.oldResource, edit.newResource].filter(Boolean).every(m.URI.isUri)),
        cancelled: options.token.isCancellationRequested }); return edits;
    }));
    const edit = new m.WorkspaceEdit();
    edit.replace(one, new m.Range(0, 0, 0, 3), 'ONE', { needsConfirmation: true, label: '真实预览' });
    edit.replace(two, new m.Range(0, 0, 0, 3), 'TWO');
    edit.createFile(created, { contents: new TextEncoder().encode('创建内容') }); edit.renameFile(before, after); edit.deleteFile(deleted);
    const applied = await a.workspace.applyEdit(edit, { isRefactoring: true });
    return { applied, values: [first.getValue(), second.getValue(), untouched.getValue()], created: await p.read(a.logical(created)),
      renamed: await p.read(a.logical(after)), missing: [await p.files.exists(a.logical(before)), await p.files.exists(a.logical(deleted))], previews,
      calls: [a.calls.length, b.calls.length], options: { refactoring: a.calls[0].options.respectAutoSaveConfig, group: a.calls[0].options.undoRedoGroupId ?? null },
      resources: a.calls[0].edits.flatMap(edit => [edit.resource, edit.oldResource, edit.newResource].filter(Boolean).map(uri => uri.toString())),
      expected: [one, two, created, before, after, deleted].map(uri => a.logical(uri).toString()),
      released: p.resolutions.every(value => value.disposed), operations: p.operations.map(value => [value.phase, value.operation]),
      expectedOperations: [m.FileOperation.CREATE, m.FileOperation.MOVE, m.FileOperation.DELETE].flatMap(operation => [['will', operation], ['did', operation]]), warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(true); expect(result.values).toEqual(['ONE-one', 'TWO-two', 'peer-original']);
  expect(result.created).toBe('创建内容'); expect(result.renamed).toBe('rename-content'); expect(result.missing).toEqual([false, false]);
  expect(result.previews).toEqual([{ types: ['text', 'text', 'file', 'file', 'file'], contents: '创建内容', typed: true, cancelled: false }]);
  expect(result.calls).toEqual([1, 0]); expect(result.options).toEqual({ refactoring: true, group: null }); expect(result.resources).toEqual(result.expected);
  expect(result.released).toBe(true); expect(result.operations).toEqual(result.expectedOperations); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push('原公开API/转换器/RPC恢复typed URI及内容，双根真实模型编辑、create/rename/delete和peer保持');
});

test('整批后一项 foreign 的 text/create/rename 双端/delete 在任何首项写入前拒绝', async () => {
  const result = await inBrowser('foreign DTO 整批准入与全部端点', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), b = p.target('B');
    const own = a.logical(p.native(a, 0, 'own.ts')), foreign = m.URI.joinPath(b.uriTransformer.folders[0].uri, 'foreign.ts');
    const model = p.openText(a, p.native(a, 0, 'own.ts'), 'old-own'); await p.seed(own, 'own-file'); await p.seed(foreign, 'foreign-file');
    const cases = [p.dtoText(foreign), p.dtoCreate(foreign), { oldResource: foreign, newResource: own },
      { oldResource: own, newResource: foreign }, { oldResource: foreign }, { ...p.dtoText(own), oldResource: foreign, newResource: own }];
    const results = [], firstResources = [];
    for (const [index, invalid] of cases.entries()) {
      const first = m.URI.joinPath(a.host.uriTransformer.folders[0].uri, `must-not-create-${index}.ts`); firstResources.push(first);
      // typed foreign 在真实 URI transport 解析时即被拒绝；这里直接进入原 MainThread，单独证明其整批防线。
      results.push(await a.main.$tryApplyWorkspaceEdit(new m.SerializableObjectWithBuffers({ edits: [p.dtoCreate(first), p.dtoText(own), invalid] })));
    }
    return { results, model: model.getValue(), contents: [await p.read(own), await p.read(foreign)],
      firstExists: await Promise.all(firstResources.map(uri => p.files.exists(uri))), calls: a.calls.length, resolutions: p.resolutions.length,
      operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.results).toEqual([false, false, false, false, false, false]); expect(result.model).toBe('old-own'); expect(result.contents).toEqual(['own-file', 'foreign-file']);
  expect(result.firstExists).toEqual([false, false, false, false, false, false]); expect(result.calls).toBe(0); expect(result.resolutions).toBe(0); expect(result.operations).toEqual([]);
  expect(result.warnings).toHaveLength(6); for (const warning of result.warnings) { expect(warning).toContain('IGNORING workspace edit:'); expect(warning).toContain('超出发起宿主工作区视图'); }
  expect(result.failures).toEqual([]); checks.push('原 MainThread 逐批拒绝 foreign resource/oldResource/newResource，合法首项零模型/文件操作');
});

test('允许经过 URI transport 的 app 位置仍由原 MainThread 整批拒绝', async () => {
  const result = await inBrowser('原公开API和RPC中的非法app编辑', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), own = p.native(a, 0, 'source.ts');
    const model = p.openText(a, own, 'old-source'), app = m.URI.file('/hosts/A/app/outside.ts'), results = [], firstResources = [];
    const append = [edit => edit.replace(app, new m.Range(0, 0, 0, 3), 'bad'), edit => edit.createFile(app, { contents: new Uint8Array([1]) }),
      edit => edit.renameFile(app, own), edit => edit.renameFile(own, app), edit => edit.deleteFile(app)];
    for (const [index, invalid] of append.entries()) {
      const first = p.native(a, 1, `rpc-must-not-create-${index}.ts`), edit = new m.WorkspaceEdit(); firstResources.push(a.logical(first));
      edit.createFile(first, { contents: new Uint8Array([2]) }); invalid(edit); results.push(await a.workspace.applyEdit(edit));
    }
    return { results, model: model.getValue(), exists: await Promise.all(firstResources.map(uri => p.files.exists(uri))), calls: a.calls.length,
      resolutions: p.resolutions.length, operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.results).toEqual([false, false, false, false, false]); expect(result.model).toBe('old-source'); expect(result.exists).toEqual([false, false, false, false, false]);
  expect(result.calls).toBe(0); expect(result.resolutions).toBe(0); expect(result.operations).toEqual([]); expect(result.warnings).toHaveLength(5);
  for (const warning of result.warnings) { expect(warning).toContain('IGNORING workspace edit:'); expect(warning).toContain('file:///hosts/A/app/outside.ts'); }
  expect(result.failures).toEqual([]); checks.push('真实RPC通过app transport后，原MainThread在bulk.apply前整批拒绝文本和文件全部端点');
});

test('具名 profile 与 workspace 配置保留原资源，配置父位置和另一宿主配置拒绝', async () => {
  const result = await inBrowser('精确配置资源编辑与父位置拒绝', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), b = p.target('B');
    const resources = [a.host.binding.initialization.environment.remote_settings, a.host.binding.initialization.workspace.configuration];
    const native = resources.map(value => m.URI.parse(value.native_uri)), models = native.map(uri => p.openText(a, uri, 'old-config'));
    const edit = new m.WorkspaceEdit(); for (const uri of native) edit.replace(uri, new m.Range(0, 0, 0, 3), 'new');
    const applied = await a.workspace.applyEdit(edit), callsAfterSuccess = a.calls.length, rejected = [];
    for (const uri of native) { const invalid = new m.WorkspaceEdit(); invalid.replace(m.dirname(uri), new m.Range(0, 0, 0, 0), 'bad'); rejected.push(await a.workspace.applyEdit(invalid)); }
    rejected.push(await a.main.$tryApplyWorkspaceEdit(new m.SerializableObjectWithBuffers({ edits: [p.dtoText(m.URI.parse(b.binding.initialization.environment.remote_settings.resource_uri))] })));
    return { applied, values: models.map(model => model.getValue()), resources: a.calls[0].edits.map(value => value.resource.toString()),
      expected: resources.map(value => value.resource_uri), rejected, extraCalls: a.calls.length - callsAfterSuccess, warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(true); expect(result.values).toEqual(['new-config', 'new-config']); expect(result.resources).toEqual(result.expected);
  expect(result.rejected).toEqual([false, false, false]); expect(result.extraCalls).toBe(0); expect(result.warnings).toHaveLength(3); expect(result.failures).toEqual([]);
  checks.push('具名profile/workspace配置经原native映射编辑真实模型，父位置和另一宿主配置零新增bulk调用');
});

test('无 binding 的 Web 保留原 file 与 untitled 文本范围及文件创建', async () => {
  const result = await inBrowser('Web原编辑资源范围', async () => {
    const p = globalThis.editProbe, m = p.m, web = await p.connect({ runningLocation: { kind: 2 } });
    const file = m.URI.file('/outside-web/original.ts'), untitled = m.URI.parse('untitled:web-edit.ts'), created = m.URI.file('/outside-web/created.ts');
    const one = p.openText(web, file, 'old-file'), two = p.openText(web, untitled, 'old-untitled'); await p.files.createFolder(m.dirname(created));
    const edit = new m.WorkspaceEdit(); edit.replace(file, new m.Range(0, 0, 0, 3), 'web'); edit.replace(untitled, new m.Range(0, 0, 0, 3), 'web');
    edit.createFile(created, { contents: new TextEncoder().encode('web-created') });
    return { applied: await web.workspace.applyEdit(edit), values: [one.getValue(), two.getValue()], created: await p.read(created),
      resources: web.calls[0].edits.map(value => (value.resource ?? value.newResource).toString()), expected: [file, untitled, created].map(uri => uri.toString()), warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(true); expect(result.values).toEqual(['web-file', 'web-untitled']); expect(result.created).toBe('web-created'); expect(result.resources).toEqual(result.expected);
  expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push('Web无binding时保留原file/untitled模型及内存文件创建范围');
});

test('原分组版本错误与 DTO revive 错误仍记录原日志并返回 false', async () => {
  const result = await inBrowser('保留原错误日志和false', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'version.ts');
    const model = p.openText(a, native, 'old-value'), expectedVersion = model.getVersionId();
    model.applyEdits([{ range: model.getFullModelRange(), text: 'newer-value' }]);
    const edit = new m.WorkspaceEdit(); edit.replace(native, new m.Range(0, 0, 0, 3), 'bad');
    const stale = await a.workspace.applyEdit(edit), beforeRevive = a.calls.length;
    const created = p.native(a, 0, 'unresolved-transfer.ts');
    const revive = await a.remote.$tryApplyWorkspaceEdit(new m.SerializableObjectWithBuffers({ edits: [{ newResource: created,
      options: { contents: { type: 'dataTransferItem', id: 'missing-transfer' } } }] }));
    return { stale, revive, value: model.getValue(), expectedVersion, currentVersion: model.getVersionId(), versionInDto: a.calls[0].edits[0].versionId,
      reviveCalls: a.calls.length - beforeRevive, exists: await p.files.exists(a.logical(created)), resolutions: p.resolutions.length, operations: p.operations,
      warnings: p.warnings, failures: p.failures };
  });
  expect(result.stale).toBe(false); expect(result.revive).toBe(false); expect(result.value).toBe('newer-value'); expect(result.currentVersion).toBeGreaterThan(result.expectedVersion);
  expect(result.versionInDto).toBe(result.expectedVersion); expect(result.reviveCalls).toBe(0); expect(result.exists).toBe(false); expect(result.resolutions).toBe(0); expect(result.operations).toEqual([]);
  expect(result.warnings).toHaveLength(2); expect(result.warnings[0]).toContain('IGNORING workspace edit:'); expect(result.warnings[0]).toContain('has changed in the meantime');
  expect(result.warnings[1]).toContain('IGNORING workspace edit:'); expect(result.warnings[1]).toContain('Could not revive data transfer file');
  expect(result.failures).toHaveLength(1); expect(result.failures[0]).toContain('has changed in the meantime');
  // TODO: 这里只证明原 BulkTextEdits 分组版本校验，整批跨类型版本预检仍由产品 owner 迁齐。
  checks.push('原分组版本校验和revive错误保留原warn/false，不写模型或文件；不代表整批版本原子性');
});

test('预览等待时 dispose 取消原 token，迟到预览零写入且新编辑不进入原 bulk.apply', async () => {
  const result = await inBrowser('预览撤代零实际操作和false', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), release = p.defer();
    const native = p.native(a, 0, 'preview.ts'), model = p.openText(a, native, 'old-preview'), created = p.native(a, 1, 'preview-created.ts'), later = p.native(a, 1, 'after-dispose.ts');
    let previewToken, classes;
    p.lifetime.add(a.actualBulk.setPreviewHandler(async (edits, options) => {
      previewToken = options.token; classes = edits.map(edit => edit instanceof m.ResourceTextEdit ? 'text' : edit instanceof m.ResourceFileEdit ? 'file' : 'other');
      entered.resolve(); await release.promise; return edits;
    }));
    const edit = new m.WorkspaceEdit(); edit.replace(native, new m.Range(0, 0, 0, 3), 'bad', { needsConfirmation: true, label: '撤代预览' });
    edit.createFile(created, { contents: new Uint8Array([1]) }); const pending = a.workspace.applyEdit(edit);
    await Promise.race([entered.promise, pending.then(() => { throw new Error('编辑在进入原预览前已结束'); })]);
    const before = { value: model.getValue(), exists: await p.files.exists(a.logical(created)), operations: p.operations.length, calls: a.calls.length };
    a.main.dispose(); const cancelled = previewToken.isCancellationRequested;
    const late = new m.WorkspaceEdit(); late.createFile(later, { contents: new Uint8Array([2]) }); const rejected = await a.workspace.applyEdit(late);
    release.resolve(); const applied = await pending;
    return { before, cancelled, classes, rejected, applied, value: model.getValue(), exists: [await p.files.exists(a.logical(created)), await p.files.exists(a.logical(later))],
      calls: a.calls.length, resolutions: p.resolutions.length, operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.before).toEqual({ value: 'old-preview', exists: false, operations: 0, calls: 1 }); expect(result.cancelled).toBe(true); expect(result.classes).toEqual(['text', 'file']);
  expect(result.rejected).toBe(false); expect(result.applied).toBe(false); expect(result.value).toBe('old-preview'); expect(result.exists).toEqual([false, false]);
  expect(result.calls).toBe(1); expect(result.resolutions).toBe(0); expect(result.operations).toEqual([]); expect(result.warnings).toHaveLength(1); expect(result.warnings[0]).toContain('所属宿主代际已经关闭');
  expect(result.failures).toEqual([]); checks.push('原BulkEditService预览gate迟到后取消token无资源，返回false；dispose后零新增bulk/模型/文件操作');
});

test('未取消的原空批次、no-op 与预览过滤结果保持原 isApplied 语义', async () => {
  const result = await inBrowser('非取消原isApplied行为', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'noop.ts'), model = p.openText(a, native, 'old-noop');
    const version = model.getVersionId(), empty = await a.workspace.applyEdit(new m.WorkspaceEdit()), noopEdit = new m.WorkspaceEdit();
    noopEdit.insert(native, new m.Position(0, 0), ''); const noop = await a.workspace.applyEdit(noopEdit);
    p.lifetime.add(a.actualBulk.setPreviewHandler(async () => []));
    const filteredEdit = new m.WorkspaceEdit(); filteredEdit.replace(native, new m.Range(0, 0, 0, 3), 'bad', { needsConfirmation: true, label: '过滤全部' });
    const filtered = await a.workspace.applyEdit(filteredEdit);
    return { empty, noop, filtered, value: model.getValue(), version, currentVersion: model.getVersionId(), calls: a.calls.length,
      tokens: a.calls.map(call => call.options.token.isCancellationRequested), resolutions: p.resolutions, operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.empty).toBe(false); expect(result.noop).toBe(true); expect(result.filtered).toBe(false); expect(result.value).toBe('old-noop'); expect(result.currentVersion).toBe(result.version);
  expect(result.calls).toBe(3); expect(result.tokens).toEqual([false, false, false]); expect(result.resolutions).toHaveLength(1); expect(result.resolutions[0].disposed).toBe(true);
  expect(result.operations).toEqual([]); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push('非取消empty=false、原无实际资源no-op=true、预览过滤空=false保持');
});

test('真实文件已完成但原回执仍等待时 dispose，保留已完成资源的成功结果', async () => {
  const result = await inBrowser('实际写入完成后撤代保留结果', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), release = p.defer(), native = p.native(a, 0, 'completed.ts'), resource = a.logical(native);
    p.lifetime.add(p.workingFiles.onDidRunWorkingCopyFileOperation(event => {
      if (event.operation === m.FileOperation.CREATE && event.files.some(file => file.target.toString() === resource.toString())) {
        event.waitUntil(release.promise); entered.resolve();
      }
    }));
    const edit = new m.WorkspaceEdit(); edit.createFile(native, { contents: new TextEncoder().encode('already-created') });
    const pending = a.workspace.applyEdit(edit);
    await Promise.race([entered.promise, pending.then(() => { throw new Error('编辑在原文件完成事件前已结束'); })]);
    const before = { content: await p.read(resource), operations: p.operations.length, calls: a.calls.length };
    a.main.dispose(); const cancelled = a.calls[0].options.token.isCancellationRequested; release.resolve();
    return { before, cancelled, applied: await pending, content: await p.read(resource), calls: a.calls.length,
      operations: p.operations.map(value => [value.phase, value.operation]), expectedOperations: [['will', m.FileOperation.CREATE], ['did', m.FileOperation.CREATE]], warnings: p.warnings, failures: p.failures };
  });
  expect(result.before).toEqual({ content: 'already-created', operations: 2, calls: 1 }); expect(result.cancelled).toBe(true); expect(result.applied).toBe(true);
  expect(result.content).toBe('already-created'); expect(result.calls).toBe(1); expect(result.operations).toEqual(result.expectedOperations); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push('原WorkingCopyFileService完成真实内存文件后等待did回执，撤代保留原实际资源成功');
});
