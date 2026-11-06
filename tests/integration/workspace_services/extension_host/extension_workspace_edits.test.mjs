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
export { ResourceNotebookCellEdit } from '${upstream}workbench/contrib/bulkEdit/browser/bulkCellEdits.js';
export { ResourceAttachmentEdit } from '${upstream}workbench/contrib/bulkEdit/browser/opaqueEdits.js';
export { CellEditType, CellUri } from '${upstream}workbench/contrib/notebook/common/notebookCommon.js';
export { INotebookEditorModelResolverService } from '${upstream}workbench/contrib/notebook/common/notebookEditorModelResolverService.js';
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
export { CancellationTokenSource } from '${upstream}base/common/cancellation.js';
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
  'upstream/src/vs/workbench/contrib/bulkEdit/browser/bulkCellEdits.ts', 'upstream/src/vs/workbench/contrib/bulkEdit/browser/opaqueEdits.ts',
  'upstream/src/vs/workbench/contrib/notebook/common/notebookCommon.ts',
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
      boundary: '真实 Chromium 中 AST 提取的原 workspace.applyEdit、ExtHostBulkEdits/转换器、RPCProtocol/SerializableObjectWithBuffers/TargetUriTransformer、MainThreadBulkEdits、BulkEditService、BulkTextEdits/ModelService/TextModel/UndoRedoService、BulkFileEdits/WorkingCopyFileService/FileService 和 OpaqueEdits。认证 binding、字节输送、文档版本镜像、文本 resolver 的请求延迟/失败/按需创建原模型、配置/UI 与纯文本 TreeSitter 库是具名装具；resolver引用只观察请求/释放，不实现原TextModelResolver的引用计数及关模/重开保护。目标 WorkspaceFiles 和配置文件 provider 明确由原 InMemoryFileSystemProvider 替代。版本测试核预览最终集合和首项类型副作用前的现有versionId/notebookVersionId；Notebook只经原版本DTO/RPC或直接ResourceEdit和具名resolver提供版本，不能证明Notebook真实模型编辑或版本producer。具名配置仅验证原资源映射和模型编辑，不代表真实配置保存、产品 WorkspaceFiles 的 rename/delete 支持、Gateway、真实 Node、磁盘保存、Windows、无版本字段的原子性或所有取消时点。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
  }
});

async function inBrowser(name, body, argument) {
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
      // resolver 只提供原 TextModel；引用释放只作观察，不模拟原 resolver 的引用计数与关模／重开保护。
      const lazyTexts = new Map(), resolver = { requests: [], onResolve: async () => {} };
      services.set(m.ITextModelService, { async createModelReference(resource) {
        resolver.requests.push(resource.toString());
        await resolver.onResolve(resource);
        let model = models.getModel(resource);
        if (!model && lazyTexts.has(resource.toString())) {
          model = models.createModel(lazyTexts.get(resource.toString()), null, resource); lazyTexts.delete(resource.toString());
        }
        if (!model) throw new Error('装具没有载入该模型：' + resource.toString());
        const observation = { resource: resource.toString(), disposed: false, disposeCount: 0 }; resolutions.push(observation);
        return { object: { textEditorModel: model }, dispose() { observation.disposed = true; observation.disposeCount++; } };
      } });
      const notebooks = { requests: [], references: [], writes: 0, onResolve: unexpected('Notebook resolver 未配置') };
      services.set(m.INotebookEditorModelResolverService, { async resolve(resource) {
        notebooks.requests.push(resource.toString()); const notebook = await notebooks.onResolve(resource);
        const observation = { resource: resource.toString(), disposed: false, disposeCount: 0 }; notebooks.references.push(observation);
        return { object: { notebook, isReadonly: () => false }, dispose() { observation.disposed = true; observation.disposeCount++; } };
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
      function unloadedText(client, native, value) {
        // 镜像版本先取自原 TextModel；仅在原 resolver 被请求时再由原 ModelService 创建真实模型。
        const model = openText(client, native, value), version = model.getVersionId(), resource = model.uri;
        model.dispose(); lazyTexts.set(resource.toString(), value); return { resource, version };
      }
      const native = (client, rootIndex, name) => m.URI.joinPath(client.host.uriTransformer.folders[rootIndex].native, name);
      const seed = async (resource, value) => { await files.createFolder(m.dirname(resource)); await files.createFile(resource, m.VSBuffer.fromString(value)); };
      const read = async resource => (await files.readFile(resource)).value.toString();
      const dtoText = (resource, text = 'changed') => ({ resource, textEdit: { range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }, text } });
      const dtoCreate = (newResource, text = 'created') => ({ newResource, options: { contents: { type: 'base64', value: m.encodeBase64(m.VSBuffer.fromString(text)) } } });
      const notebookVersion = (resource, versionId) => ({ uri: resource, versionId,
        applyEdits() { notebooks.writes++; throw new Error('本版本冲突场景不应执行 Notebook 编辑'); } });
      const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
      globalThis.editProbe = { m, target, connect, openText, unloadedText, native, seed, read, dtoText, dtoCreate, defer, files, models, workingFiles,
        warnings, failures, resolutions, resolver, notebooks, notebookVersion, operations, lifetime,
        dispose() { for (const client of clients.reverse()) client.close(); for (const model of models.getModels()) model.dispose(); lifetime.dispose(); } };
    });
    const result = await page.evaluate(body, argument); expect(errors).toEqual([]); observations.push({ name, result }); return result;
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
  checks.push('已加载模型原版本错误和revive错误保留原warn/false，不额外解析错误版本，不写模型或文件');
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
  expect(result.calls).toBe(3); expect(result.tokens).toEqual([false, false, false]); expect(result.resolutions).toHaveLength(2);
  for (const reference of result.resolutions) { expect(reference.disposed).toBe(true); expect(reference.disposeCount).toBe(1); }
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

for (const leadingText of [false, true]) test(`预览等待中后项文本版本改变时，${leadingText ? '先前文本组和文件' : '首项文件'}均不产生副作用`, async () => {
  const result = await inBrowser('预览后整批版本预检-' + leadingText, async leadingText => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), release = p.defer();
    const first = p.native(a, 0, 'first.ts'), later = p.native(a, 1, 'later.ts'), created = p.native(a, 0, 'must-not-create.ts');
    const firstModel = p.openText(a, first, 'old-first'), laterModel = p.openText(a, later, 'old-later'), version = laterModel.getVersionId();
    let previewTypes;
    p.lifetime.add(a.actualBulk.setPreviewHandler(async edits => {
      previewTypes = edits.map(edit => edit instanceof m.ResourceTextEdit ? 'text' : 'file'); entered.resolve(); await release.promise; return edits;
    }));
    const edit = new m.WorkspaceEdit(); if (leadingText) edit.replace(first, new m.Range(0, 0, 0, 3), 'BAD');
    edit.createFile(created, { contents: new TextEncoder().encode('must-not-create') });
    edit.replace(later, new m.Range(0, 0, 0, 3), 'BAD', { needsConfirmation: true, label: '版本等待' });
    const pending = a.workspace.applyEdit(edit);
    await Promise.race([entered.promise, pending.then(() => { throw new Error('编辑未经过原预览等待'); })]);
    laterModel.applyEdits([{ range: laterModel.getFullModelRange(), text: 'newer-later' }]); release.resolve();
    return { applied: await pending, values: [firstModel.getValue(), laterModel.getValue()], exists: await p.files.exists(a.logical(created)),
      previewTypes, version, currentVersion: laterModel.getVersionId(), dtoVersion: a.calls[0].edits.at(-1).versionId,
      operations: p.operations, released: p.resolutions.every(ref => ref.disposed), warnings: p.warnings, failures: p.failures };
  }, leadingText);
  expect(result.applied).toBe(false); expect(result.values).toEqual(['old-first', 'newer-later']); expect(result.exists).toBe(false); expect(result.operations).toEqual([]);
  expect(result.previewTypes).toEqual(leadingText ? ['text', 'file', 'text'] : ['file', 'text']); expect(result.dtoVersion).toBe(result.version); expect(result.currentVersion).toBeGreaterThan(result.version);
  expect(result.released).toBe(true); expect(result.warnings).toHaveLength(1); expect(result.warnings[0]).toContain('IGNORING workspace edit:'); expect(result.warnings[0]).toContain('has changed in the meantime');
  expect(result.failures).toHaveLength(1); expect(result.failures[0]).toContain('has changed in the meantime');
  checks.push(`预览后版本冲突在${leadingText ? '前置文本组及' : ''}首file副作用前拒绝，原公开API版本DTO、真实模型变化和warn/false保留`);
});

test('预览移除版本冲突项后只核最终集合，保留项经原文件和文本 owner 成功应用', async () => {
  const result = await inBrowser('预览最终集合版本预检', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), release = p.defer();
    const good = p.native(a, 0, 'good.ts'), stale = p.native(a, 1, 'filtered.ts'), created = p.native(a, 0, 'retained.ts');
    const goodModel = p.openText(a, good, 'old-good'), staleModel = p.openText(a, stale, 'old-filtered'), staleResource = a.logical(stale).toString(), version = staleModel.getVersionId();
    let finalResources;
    p.lifetime.add(a.actualBulk.setPreviewHandler(async edits => {
      entered.resolve(); await release.promise; const final = edits.filter(edit => edit.resource?.toString() !== staleResource);
      finalResources = final.map(edit => (edit.resource ?? edit.newResource).toString()); return final;
    }));
    const edit = new m.WorkspaceEdit(); edit.createFile(created, { contents: new TextEncoder().encode('retained') });
    edit.replace(good, new m.Range(0, 0, 0, 3), 'NEW'); edit.replace(stale, new m.Range(0, 0, 0, 3), 'BAD', { needsConfirmation: true, label: '移除冲突项' });
    const pending = a.workspace.applyEdit(edit); await Promise.race([entered.promise, pending.then(() => { throw new Error('未进入原预览'); })]);
    staleModel.applyEdits([{ range: staleModel.getFullModelRange(), text: 'newer-filtered' }]); release.resolve();
    return { applied: await pending, values: [goodModel.getValue(), staleModel.getValue()], created: await p.read(a.logical(created)), finalResources,
      expected: [created, good].map(uri => a.logical(uri).toString()), dtoVersion: a.calls[0].edits.at(-1).versionId, version,
      resolvedFiltered: p.resolver.requests.includes(staleResource), released: p.resolutions.every(ref => ref.disposed), warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(true); expect(result.values).toEqual(['NEW-good', 'newer-filtered']); expect(result.created).toBe('retained'); expect(result.finalResources).toEqual(result.expected);
  expect(result.dtoVersion).toBe(result.version); expect(result.resolvedFiltered).toBe(false); expect(result.released).toBe(true); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push('原预览删除冲突项后仅最终集合参与版本核对，保留file/text成功，已过滤旧版本不再阻断');
});

test('后一个模型解析等待时前一个真实模型改变，最终同步核对在首 file 前失败并释放所有引用', async () => {
  const result = await inBrowser('异步多模型解析后的最终版本核对', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), release = p.defer();
    const first = p.native(a, 0, 'cold-first.ts'), second = p.native(a, 1, 'cold-second.ts'), created = p.native(a, 0, 'must-not-create.ts');
    const one = p.unloadedText(a, first, 'old-first'), two = p.unloadedText(a, second, 'old-second');
    p.resolver.onResolve = async resource => { if (resource.toString() === two.resource.toString()) { entered.resolve(); await release.promise; } };
    const edit = new m.WorkspaceEdit(); edit.createFile(created, { contents: new Uint8Array([1]) });
    edit.replace(first, new m.Range(0, 0, 0, 3), 'BAD'); edit.replace(second, new m.Range(0, 0, 0, 3), 'BAD');
    const pending = a.workspace.applyEdit(edit); await Promise.race([entered.promise, pending.then(() => { throw new Error('未经过第二个真实模型解析等待'); })]);
    const firstModel = p.models.getModel(one.resource); if (!firstModel) throw new Error('第一个原模型尚未由 resolver 创建');
    const before = { exists: await p.files.exists(a.logical(created)), operations: p.operations.length, references: p.resolutions.length, firstDisposed: p.resolutions[0].disposed };
    firstModel.applyEdits([{ range: firstModel.getFullModelRange(), text: 'newer-first' }]); release.resolve();
    const applied = await pending;
    return { applied, before, values: [firstModel.getValue(), p.models.getModel(two.resource).getValue()], exists: await p.files.exists(a.logical(created)),
      versions: a.calls[0].edits.filter(edit => edit.textEdit).map(edit => edit.versionId), expectedVersions: [one.version, two.version],
      references: p.resolutions, requests: p.resolver.requests, expectedResources: [one.resource, two.resource].map(uri => uri.toString()), operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.before).toEqual({ exists: false, operations: 0, references: 1, firstDisposed: false }); expect(result.applied).toBe(false); expect(result.exists).toBe(false);
  expect(result.values).toEqual(['newer-first', 'old-second']); expect(result.versions).toEqual(result.expectedVersions); expect(result.operations).toEqual([]); expect(result.requests).toEqual(result.expectedResources);
  expect(result.references.map(ref => [ref.resource, ref.disposed, ref.disposeCount])).toEqual(result.expectedResources.map(resource => [resource, true, 1]));
  expect(result.warnings).toHaveLength(1); expect(result.warnings[0]).toContain('has changed in the meantime'); expect(result.failures).toHaveLength(1); expect(result.failures[0]).toContain('has changed in the meantime');
  checks.push('原公开API的未加载模型经resolver创建，后一次解析等待期间前模型变更，首file前最终统一核对拒绝且全部引用只释放一次');
});

test('同一文本资源的多个版本编辑在首 file 前只持有一次预检引用，原文本组仍独立解析和编辑', async () => {
  const result = await inBrowser('重复文本资源的引用与原分组编辑', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'repeated.ts'), created = p.native(a, 1, 'created.ts');
    const cold = p.unloadedText(a, native, 'abcdefgh'), beforeFile = []; let heldThroughFile;
    p.lifetime.add(p.workingFiles.onWillRunWorkingCopyFileOperation(event => {
      if (event.operation === m.FileOperation.CREATE) beforeFile.push(p.resolutions.map(ref => ({ resource: ref.resource, disposed: ref.disposed })));
    }));
    p.lifetime.add(p.workingFiles.onDidRunWorkingCopyFileOperation(event => {
      if (event.operation === m.FileOperation.CREATE) heldThroughFile = p.resolutions.every(ref => !ref.disposed);
    }));
    const edit = new m.WorkspaceEdit(); edit.createFile(created, { contents: new TextEncoder().encode('created') });
    edit.replace(native, new m.Range(0, 0, 0, 1), 'A'); edit.replace(native, new m.Range(0, 3, 0, 4), 'D');
    const applied = await a.workspace.applyEdit(edit);
    return { applied, value: p.models.getModel(cold.resource).getValue(), created: await p.read(a.logical(created)), beforeFile, heldThroughFile,
      requests: p.resolver.requests, references: p.resolutions, resource: cold.resource.toString(), versions: a.calls[0].edits.filter(edit => edit.textEdit).map(edit => edit.versionId),
      version: cold.version, warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(true); expect(result.value).toBe('AbcDefgh'); expect(result.created).toBe('created'); expect(result.versions).toEqual([result.version, result.version]);
  expect(result.beforeFile).toEqual([[{ resource: result.resource, disposed: false }]]); expect(result.heldThroughFile).toBe(true); expect(result.requests).toEqual([result.resource, result.resource]);
  expect(result.references.map(ref => [ref.disposed, ref.disposeCount])).toEqual([[true, 1], [true, 1]]); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push('重复text版本项在首file前只取得一份预检引用，原BulkTextEdits随后仍独立解析一次，原编辑及两份引用回收成功');
});

for (const outcome of ['failure', 'cancel']) test(`后一个 resolver ${outcome} 时释放已有预检引用，整批零副作用`, async () => {
  const result = await inBrowser('预检解析等待的' + outcome, async outcome => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), release = p.defer();
    const first = p.native(a, 0, 'first.ts'), second = p.native(a, 1, 'second.ts'), created = p.native(a, 0, 'must-not-create.ts');
    const one = p.unloadedText(a, first, 'old-first'), two = p.unloadedText(a, second, 'old-second');
    p.resolver.onResolve = async resource => {
      if (resource.toString() === two.resource.toString()) { entered.resolve(); await release.promise; if (outcome === 'failure') throw new Error('预检第二个模型解析失败'); }
    };
    const edit = new m.WorkspaceEdit(); edit.createFile(created, { contents: new Uint8Array([1]) });
    edit.replace(first, new m.Range(0, 0, 0, 3), 'BAD'); edit.replace(second, new m.Range(0, 0, 0, 3), 'BAD');
    const pending = a.workspace.applyEdit(edit); await Promise.race([entered.promise, pending.then(() => { throw new Error('未进入第二个模型解析等待'); })]);
    const before = { exists: await p.files.exists(a.logical(created)), operations: p.operations.length, references: p.resolutions.length, disposed: p.resolutions[0].disposed };
    if (outcome === 'cancel') a.main.dispose(); const cancelled = a.calls[0].options.token.isCancellationRequested;
    release.resolve(); const applied = await pending;
    return { applied, before, cancelled, exists: await p.files.exists(a.logical(created)), values: p.models.getModels().map(model => model.getValue()).sort(),
      references: p.resolutions, requests: p.resolver.requests, expected: [one.resource, two.resource].map(uri => uri.toString()), operations: p.operations, warnings: p.warnings, failures: p.failures };
  }, outcome);
  expect(result.before).toEqual({ exists: false, operations: 0, references: 1, disposed: false }); expect(result.applied).toBe(false); expect(result.exists).toBe(false); expect(result.operations).toEqual([]);
  expect(result.cancelled).toBe(outcome === 'cancel'); expect(result.requests).toEqual(result.expected); expect(result.values).toEqual(outcome === 'failure' ? ['old-first'] : ['old-first', 'old-second']);
  expect(result.references).toHaveLength(outcome === 'failure' ? 1 : 2); for (const ref of result.references) { expect(ref.disposed).toBe(true); expect(ref.disposeCount).toBe(1); }
  expect(result.warnings).toHaveLength(outcome === 'failure' ? 1 : 0); expect(result.failures).toHaveLength(outcome === 'failure' ? 1 : 0);
  if (outcome === 'failure') { expect(result.warnings[0]).toContain('IGNORING workspace edit:'); expect(result.warnings[0]).toContain('预检第二个模型解析失败'); expect(result.failures[0]).toContain('预检第二个模型解析失败'); }
  checks.push(`原resolver第二次${outcome}后已取得引用全部只释放一次，原公开API返回false且零文本/文件副作用`);
});

test('无 versionId 的文本不增加预检读取，文件及 opaque 继续各自原语义', async () => {
  const result = await inBrowser('无版本字段保持原owner语义', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'unversioned.ts'), created = p.native(a, 1, 'created.ts');
    const model = p.openText(a, native, 'old-value'); a.documents.delete(native.toString());
    model.applyEdits([{ range: model.getFullModelRange(), text: 'newer-value' }]); const beforeFile = [];
    p.lifetime.add(p.workingFiles.onWillRunWorkingCopyFileOperation(event => {
      if (event.operation === m.FileOperation.CREATE) beforeFile.push(p.resolver.requests.length);
    }));
    const edit = new m.WorkspaceEdit(); edit.createFile(created, { contents: new TextEncoder().encode('created') }); edit.replace(native, new m.Range(0, 0, 0, 3), 'NEW');
    const applied = await a.workspace.applyEdit(edit), requests = [...p.resolver.requests]; let redos = 0, undos = 0;
    // 函数不能经扩展 RPC 序列化；opaque 只走原 BulkEditService／OpaqueEdits，回调是明确边界。
    const opaque = await a.actualBulk.apply([new m.ResourceAttachmentEdit(a.logical(native), () => { undos++; }, () => { redos++; })]);
    return { applied, value: model.getValue(), created: await p.read(a.logical(created)), versionAbsent: a.calls[0].edits.every(item => item.versionId === undefined && item.notebookVersionId === undefined),
      beforeFile, requests, resource: model.uri.toString(), opaqueApplied: opaque.isApplied, redos, undos, extraRequests: p.resolver.requests.length - requests.length,
      references: p.resolutions, warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(true); expect(result.value).toBe('NEWer-value'); expect(result.created).toBe('created'); expect(result.versionAbsent).toBe(true);
  expect(result.beforeFile).toEqual([0]); expect(result.requests).toEqual([result.resource]); expect(result.opaqueApplied).toBe(true); expect([result.redos, result.undos, result.extraRequests]).toEqual([1, 0, 0]);
  expect(result.references).toHaveLength(1); expect(result.references[0].disposed).toBe(true); expect(result.references[0].disposeCount).toBe(1); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push('原公开API无versionId文本只由原文本组解析，首file无额外预检，opaque沿原BulkEdit/OpaqueEdits执行且不编造版本或resolver读取');
});

test('入口已取消的版本编辑不请求 resolver，也不开始任何文本或文件副作用', async () => {
  const result = await inBrowser('版本预检入口取消', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'cancelled.ts'), created = a.logical(p.native(a, 1, 'must-not-create.ts'));
    const model = p.openText(a, native, 'old-value'), version = model.getVersionId(), source = new m.CancellationTokenSource(); source.cancel();
    const applied = await a.actualBulk.apply([new m.ResourceFileEdit(undefined, created, { contents: m.VSBuffer.fromString('bad') }),
      new m.ResourceTextEdit(model.uri, p.dtoText(model.uri, 'BAD').textEdit, version)], { token: source.token }); source.dispose();
    return { applied: applied.isApplied, value: model.getValue(), exists: await p.files.exists(created), requests: p.resolver.requests, references: p.resolutions, operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(false); expect(result.value).toBe('old-value'); expect(result.exists).toBe(false); expect(result.requests).toEqual([]); expect(result.references).toEqual([]);
  expect(result.operations).toEqual([]); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push('原BulkEditService入口已取消时零版本解析、零file/text副作用且isApplied=false');
});

test('同资源的文本与 Notebook 版本分别核对，重复 Notebook 后项冲突在首 file 前拒绝', async () => {
  const result = await inBrowser('Notebook已有版本字段整批门禁与重复资源', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'shared.ipynb'), resource = a.logical(native), created = p.native(a, 1, 'must-not-create.ts');
    const model = p.openText(a, native, 'old-text'), version = model.getVersionId(), notebook = p.notebookVersion(resource, version + 10);
    p.notebooks.onResolve = async () => notebook;
    // 现有宿主未产出 Notebook 版本；显式 DTO 仅证明已有字段经原 RPC/MainThread/BulkEdit 的门禁。
    const applied = await a.remote.$tryApplyWorkspaceEdit(new m.SerializableObjectWithBuffers({ edits: [p.dtoCreate(created),
      { ...p.dtoText(native, 'BAD'), versionId: version },
      { resource: native, notebookVersionId: notebook.versionId, cellEdit: { editType: m.CellEditType.DocumentMetadata, metadata: { first: true } } },
      { resource: native, notebookVersionId: notebook.versionId - 1, cellEdit: { editType: m.CellEditType.DocumentMetadata, metadata: { late: true } } },
    ] }));
    return { applied, value: model.getValue(), exists: await p.files.exists(a.logical(created)), notebookWrites: p.notebooks.writes,
      textReferences: p.resolutions, notebookReferences: p.notebooks.references, textRequests: p.resolver.requests, notebookRequests: p.notebooks.requests, resource: resource.toString(),
      versions: a.calls[0].edits.map(edit => edit.versionId ?? edit.notebookVersionId ?? null), expectedVersions: [null, version, notebook.versionId, notebook.versionId - 1],
      operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied).toBe(false); expect(result.value).toBe('old-text'); expect(result.exists).toBe(false); expect(result.notebookWrites).toBe(0); expect(result.operations).toEqual([]);
  expect(result.versions).toEqual(result.expectedVersions); expect(result.textRequests).toEqual([result.resource]); expect(result.notebookRequests).toEqual([result.resource]);
  for (const references of [result.textReferences, result.notebookReferences]) { expect(references).toHaveLength(1); expect(references[0].disposed).toBe(true); expect(references[0].disposeCount).toBe(1); }
  expect(result.warnings).toHaveLength(1); expect(result.warnings[0]).toContain('IGNORING workspace edit:'); expect(result.warnings[0]).toContain('Notebook'); expect(result.warnings[0]).toContain('has changed in the meantime');
  expect(result.failures).toHaveLength(1); expect(result.failures[0]).toContain('Notebook'); checks.push('原版本DTO/RPC分别核同URI文本与Notebook，Notebook重复resource单引用但逐项核版本，后项冲突在首file前零副作用');
});

test('Notebook cell URI 通过原 CellUri 归一，版本冲突在文件及 Notebook 写入前失败', async () => {
  const result = await inBrowser('Notebook cell已有版本字段归一', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), resource = a.logical(p.native(a, 0, 'cells.ipynb')), cell = m.CellUri.generate(resource, 7), created = a.logical(p.native(a, 1, 'must-not-create.ts'));
    const notebook = p.notebookVersion(resource, 4); p.notebooks.onResolve = async () => notebook;
    let error;
    try { await a.actualBulk.apply([new m.ResourceFileEdit(undefined, created, { contents: m.VSBuffer.fromString('bad') }),
      new m.ResourceNotebookCellEdit(cell, { editType: m.CellEditType.DocumentMetadata, metadata: { changed: true } }, notebook.versionId - 1)]); }
    catch (failure) { error = failure.message; }
    return { error, exists: await p.files.exists(created), requests: p.notebooks.requests, references: p.notebooks.references,
      resource: resource.toString(), writes: p.notebooks.writes, textRequests: p.resolver.requests, operations: p.operations, failures: p.failures };
  });
  expect(result.error).toContain('Notebook'); expect(result.error).toContain('has changed in the meantime'); expect(result.exists).toBe(false); expect(result.writes).toBe(0); expect(result.operations).toEqual([]);
  expect(result.requests).toEqual([result.resource]); expect(result.references).toHaveLength(1); expect(result.references[0].disposed).toBe(true); expect(result.references[0].disposeCount).toBe(1); expect(result.textRequests).toEqual([]);
  expect(result.failures).toHaveLength(1); checks.push('直接原ResourceEdit的cell URI由原CellUri归一到Notebook resolver，已有版本冲突首file前失败；不代表Notebook版本producer或真实编辑');
});
