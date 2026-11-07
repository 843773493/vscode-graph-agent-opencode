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
async function originalNodes(relative, select, artifact, print = (node, source) => node.getText(source)) {
  const path = join(root, 'upstream/src/vs', relative);
  const source = ts.createSourceFile(path, await transform(path, await readFile(path, 'utf8')), ts.ScriptTarget.Latest, true), nodes = [];
  const visit = node => { if (select(node, source)) nodes.push(print(node, source)); ts.forEachChild(node, visit); };
  visit(source);
  if (nodes.length !== 1) throw new Error(artifact + ' 原入口数量已变化：' + nodes.length);
  await writeFile(join(run.artifactsDir, artifact + '.ts'), nodes[0] + '\n'); return nodes[0];
}
const originalClass = name => node => ts.isClassDeclaration(node) && node.name?.text === name;
const originalMethod = (owner, name) => node => ts.isMethodDeclaration(node) && node.name.getText() === name && node.parent.name?.text === owner;
const renamePath = 'editor/contrib/rename/browser/rename.ts', inlinePath = 'editor/contrib/inlineCompletions/browser/model/renameSymbolProcessor.ts';
const previewPath = 'workbench/contrib/bulkEdit/browser/preview/bulkEdit.contribution.ts', panePath = 'workbench/contrib/bulkEdit/browser/preview/bulkEditPane.ts';
const skeleton = await originalNodes(renamePath, originalClass('RenameSkeleton'), 'original-rename-skeleton');
const controllerRun = await originalNodes(renamePath, originalMethod('RenameController', 'run'), 'original-rename-controller-run');
const inlineState = await originalNodes(inlinePath, originalClass('EditorState'), 'original-inline-editor-state');
const inlineRunnable = await originalNodes(inlinePath, originalClass('RenameSymbolRunnable'), 'original-inline-rename-runnable');
const previewHandler = await originalNodes(previewPath, node => ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
  && node.expression.name.text === 'setPreviewHandler', 'original-preview-handler');
const previewMethod = await originalNodes(previewPath, originalMethod('BulkEditPreviewContribution', '_previewEdit'), 'original-preview-method');
const previewSession = await originalNodes(previewPath, originalClass('PreviewSession'), 'original-preview-session');
const getPane = await originalNodes(previewPath, node => ts.isFunctionDeclaration(node) && node.name?.text === 'getBulkEditPane', 'original-preview-get-pane');
const paneInput = await originalNodes(panePath, originalMethod('BulkEditPane', 'setInput'), 'original-preview-pane-input');
const paneDone = await originalNodes(panePath, originalMethod('BulkEditPane', '_done'), 'original-preview-pane-done');
const paneOpen = await originalNodes(panePath, originalMethod('BulkEditPane', '_openElementInMultiDiffEditor'), 'original-preview-pane-open');
const providerClass = await originalNodes('workbench/contrib/bulkEdit/browser/preview/bulkEditPreview.ts', originalClass('BulkEditPreviewProvider'), 'original-preview-provider',
  (node, source) => ts.createPrinter().printNode(ts.EmitHint.Unspecified, ts.factory.updateClassDeclaration(node,
    node.modifiers?.filter(modifier => modifier.kind !== ts.SyntaxKind.ExportKeyword), node.name, node.typeParameters, node.heritageClauses, node.members), source));
const built = await build({ stdin: { loader: 'ts', resolveDir: resolve('.'), contents: `
import * as nls from '${upstream}nls.js';
import { localize } from '${upstream}nls.js';
import { CancellationToken, CancellationTokenSource } from '${upstream}base/common/cancellation.js';
import { CancellationError, onUnexpectedError } from '${upstream}base/common/errors.js';
import { DisposableStore } from '${upstream}base/common/lifecycle.js';
import { Event } from '${upstream}base/common/event.js';
import { URI } from '${upstream}base/common/uri.js';
import { generateUuid } from '${upstream}base/common/uuid.js';
import { Schemas } from '${upstream}base/common/network.js';
import { MicrotaskDelay } from '${upstream}base/common/symbols.js';
import { createTextBufferFactoryFromSnapshot } from '${upstream}editor/common/model/textModel.js';
import { ILanguageService } from '${upstream}editor/common/languages/language.js';
import { IModelService } from '${upstream}editor/common/services/model.js';
import { ITextModelService } from '${upstream}editor/common/services/resolverService.js';
import { raceCancellation } from '${upstream}base/common/async.js';
import { isMarkdownString } from '${upstream}base/common/htmlContent.js';
import { Range } from '${upstream}editor/common/core/range.js';
import { ResourceTextEdit } from '${upstream}editor/browser/services/bulkEditService.js';
import { TextReplacement } from '${upstream}editor/common/core/edits/textEdit.js';
import { EditSources } from '${upstream}editor/common/textModelEditSource.js';
import { NewSymbolNameTriggerKind } from '${upstream}editor/common/languages.js';
import { EditorStateCancellationTokenSource, CodeEditorStateFlag } from '${upstream}editor/contrib/editorState/browser/editorState.js';
import { MessageController } from '${upstream}editor/contrib/message/browser/messageController.js';
import { rawRename } from '${upstream}editor/contrib/rename/browser/rename.js';
import Severity from '${upstream}base/common/severity.js';
import { FileElement, TextEditElement } from '${upstream}workbench/contrib/bulkEdit/browser/preview/bulkEditTree.js';
import { ACTIVE_GROUP, SIDE_GROUP } from '${upstream}workbench/services/editor/common/editorService.js';
${skeleton}
${inlineState}
${inlineRunnable}
export { EditorState as InlineEditorState, RenameSymbolRunnable, CancellationToken, rawRename, EditSources, Range as EditorRange };
export function originalRenameController(fields, alert) { return Object.assign({ ${controllerRun} }, fields); }
// 仅将原类置于词法队列边界中；测试手动调度原 callback，类的方法和退役逻辑不改写。
export function originalPreviewProvider(operations, language, models, resolver, queueMicrotask) {
  ${providerClass}
  return new BulkEditPreviewProvider(operations, language, models, resolver);
}
export { FileElement, TextEditElement };
// View、UX、input 创建和树 UI 是具名边界；handler、session、setInput、取消收尾均执行转换后的原方法。
const State = { Data: 'data', Message: 'message' };
const BulkFileOperations = { create: Symbol('具名预览输入创建边界') };
class BulkEditPreviewProvider {}
class UXState { constructor(panels, groups) { this.panels = panels; this.groups = groups; } restore(...args) { return this.panels.restore(...args); } }
export class BulkEditPane {
  static ID = 'test.original-refactor-preview';
  static Schema = 'vscode-bulkeditpreview-multieditor';
  constructor(fields) { Object.assign(this, fields); }
  hasInput() { return Boolean(this._currentInput); }
  ${paneInput}
  ${paneDone}
  ${paneOpen}
}
${getPane}
${previewSession}
export { PreviewSession };
export class OriginalPreviewContribution {
  constructor(fields, bulkEditService) { Object.assign(this, fields); ${previewHandler}; }
  ${previewMethod}
}
export function originalWorkspaceApi(extHostBulkEdits, extension) { return { ${applyEditMethod} }; }
export { MainThreadBulkEdits } from '${upstream}workbench/api/browser/mainThreadBulkEdits.js';
export { MainThreadLanguageFeatures } from '${upstream}workbench/api/browser/mainThreadLanguageFeatures.js';
export { MainThreadCommands } from '${upstream}workbench/api/browser/mainThreadCommands.js';
export { LanguageFeaturesService } from '${upstream}editor/common/services/languageFeaturesService.js';
export { ILanguageFeaturesService } from '${upstream}editor/common/services/languageFeatures.js';
export { IAiEditTelemetryService } from '${upstream}workbench/contrib/editTelemetry/browser/telemetry/aiEditTelemetry/aiEditTelemetryService.js';
export { RenameSymbolProcessor } from '${upstream}editor/contrib/inlineCompletions/browser/model/renameSymbolProcessor.js';
export { renameSymbolCommandId } from '${upstream}editor/contrib/inlineCompletions/browser/controller/commandIds.js';
export { CommandsRegistry } from '${upstream}platform/commands/common/commands.js';
export { errorHandler, setUnexpectedErrorHandler, isCancellationError } from '${upstream}base/common/errors.js';
export { TextModelResolverService } from '${upstream}workbench/services/textmodelResolver/common/textModelResolverService.js';
export { BulkFileOperations as OriginalBulkFileOperations } from '${upstream}workbench/contrib/bulkEdit/browser/preview/bulkEditPreview.js';
export { BulkFileOperation as OriginalBulkFileOperation } from '${upstream}workbench/contrib/bulkEdit/browser/preview/bulkEditPreview.js';
export { IAccessibilityService } from '${upstream}platform/accessibility/common/accessibility.js';
export { ILanguageDetectionService } from '${upstream}workbench/services/languageDetection/common/languageDetectionWorkerService.js';
export { Position as EditorPosition } from '${upstream}editor/common/core/position.js';
export { Selection } from '${upstream}editor/common/core/selection.js';
export { ExtHostBulkEdits } from '${upstream}workbench/api/common/extHostBulkEdits.js';
export { ExtHostDocumentsAndEditors } from '${upstream}workbench/api/common/extHostDocumentsAndEditors.js';
export { ExtHostDocuments } from '${upstream}workbench/api/common/extHostDocuments.js';
export { ExtHostNotebookController } from '${upstream}workbench/api/common/extHostNotebook.js';
export { NotebookEdit } from '${upstream}workbench/api/common/extHostTypes.js';
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
export { MainContext, ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { SerializableObjectWithBuffers } from '${upstream}workbench/services/extensions/common/proxyIdentifier.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { CancellationTokenSource };
export { URI } from '${upstream}base/common/uri.js';
export { dirname } from '${upstream}base/common/resources.js';
export { VSBuffer, encodeBase64 } from '${upstream}base/common/buffer.js';
export { TargetUriTransformer } from './src/clients/workbench/src/extensions/targetUris.js';
export { unpackCommandSource, executeCommandForHost } from './src/clients/workbench/src/extensions/commands.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' }, plugins: [buildSourcePlugin(root, transform)],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const sources = ['scripts/sourceTransform.mjs', 'scripts/extensions/extensionFileEventsTransform.mjs', 'scripts/extensions/extensionLanguageFeaturesTransform.mjs',
  'scripts/extensions/extensionOwnerTransform.mjs', 'src/extensions/targetUris.ts', 'src/extensions/commands.ts',
  'upstream/src/vs/workbench/api/common/extHost.api.impl.ts', 'upstream/src/vs/workbench/api/common/extHostBulkEdits.ts',
  'upstream/src/vs/workbench/api/common/extHostNotebook.ts', 'upstream/src/vs/workbench/api/common/extHostNotebookDocument.ts',
  'upstream/src/vs/workbench/api/common/extHostTypeConverters.ts', 'upstream/src/vs/workbench/api/common/extHostTypes/workspaceEdit.ts',
  'upstream/src/vs/workbench/api/browser/mainThreadBulkEdits.ts', 'upstream/src/vs/workbench/contrib/bulkEdit/browser/bulkEditService.ts',
  'upstream/src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts', 'upstream/src/vs/workbench/api/browser/mainThreadCommands.ts',
  'upstream/src/vs/editor/common/languages.ts', 'upstream/src/vs/editor/common/services/languageFeaturesService.ts',
  'upstream/src/vs/editor/common/languageFeatureRegistry.ts', 'upstream/src/vs/editor/common/languageSelector.ts',
  'upstream/src/vs/editor/contrib/rename/browser/rename.ts', 'upstream/src/vs/editor/contrib/editorState/browser/editorState.ts',
  'upstream/src/vs/editor/contrib/editorState/browser/keybindingCancellation.ts',
  'upstream/src/vs/editor/contrib/inlineCompletions/browser/model/renameSymbolProcessor.ts',
  'upstream/src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEdit.contribution.ts',
  'upstream/src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPane.ts',
  'upstream/src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPreview.ts',
  'upstream/src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditTree.ts',
  'upstream/src/vs/workbench/contrib/bulkEdit/browser/conflicts.ts',
  'upstream/src/vs/workbench/services/textmodelResolver/common/textModelResolverService.ts',
  'upstream/src/vs/workbench/common/editor/textResourceEditorModel.ts',
  'upstream/src/vs/base/common/cancellation.ts', 'upstream/src/vs/base/common/errors.ts', 'upstream/src/vs/base/common/lifecycle.ts',
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
      boundary: '真实 Chromium 中 AST 提取的原 workspace.applyEdit、RenameSkeleton/Controller.run、inline EditorState/RenameSymbolRunnable、preview handler/_previewEdit/PreviewSession/getBulkEditPane/Pane.setInput/_done/_openElementInMultiDiffEditor 和完整 BulkEditPreviewProvider 类；原 RenameSymbolProcessor 注册并执行真实 CommandsRegistry 命令，原 MainThreadLanguageFeatures/共享 registry/rawRename 的同一返回对象直接交原 BulkEditService。公开 Rename 命令经原 MainThreadCommands/真实 RPC 返回，核窗口不可枚举 lifecycleToken 不进入 DTO。另运行原 ExtHostBulkEdits/转换器、RPCProtocol/SerializableObjectWithBuffers/TargetUriTransformer、MainThreadBulkEdits、BulkTextEdits/ModelService/TextModel/UndoRedoService、BulkFileEdits/WorkingCopyFileService/FileService 和 OpaqueEdits。认证 binding、字节输送、扩展 Rename 回调/已冻结版本 DTO、文档版本镜像、常规文本 resolver 延迟/失败/按需创建原模型、编辑器/Widget/通知/进度、preview View/UX/输入创建和树UI、配置与纯文本TreeSitter库是具名装具；状态取消实际运行原EditorStateCancellationTokenSource，readonly编辑器使原BulkTextEdits选择原模型任务。常规编辑组resolver只观察请求/释放，不证明其引用计数及关模/重开保护；Provider窄组另运行原BulkFileOperations/ConflictDetector、TextModelResolverService/AsyncReferenceCollection/TextResourceEditorModel，源模型持原baseline引用，resolver包装仅注入回执延迟/失败并观察原引用，词法queueMicrotask仅收集并调度原callback，不改写Provider方法或伪造模型销毁。语言检测/辅助通知和Pane资源计算/openEditor为具名边界；Pane核两次await后的input/provider精确身份及fresh成功。目标文件和配置provider由原InMemoryFileSystemProvider替代。Notebook新增原Controller的delta版本生产，经公开applyEdit和RPC核整批冲突零写入；目标模型仍由具名resolver提供，不证明真实Notebook编辑或目标同步。本片不重复原RenameAdapter版本producer，不验证inline推断/NES命令、真实Preview渲染/UX恢复、Chat分项移交后的owner、产品WorkspaceFiles rename/delete、真实配置保存、Gateway、真实Node、磁盘保存、Windows、无版本字段原子性或所有取消时点。',
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
      const undoRedo = new m.UndoRedoService(dialog, notification), language = lifetime.add(new m.LanguageService()), features = new m.LanguageFeaturesService();
      const extensions = { getExtensionHosts: () => clients.map(client => client.host), getHostExtensions: () => [], whenInstalledExtensionsRegistered: async () => {} };
      const services = new m.ServiceCollection([m.ILogService, log], [m.IFileService, files], [m.IUriIdentityService, identity],
        [m.IWorkingCopyService, workingCopies], [m.IConfigurationService, config], [m.IUndoRedoService, undoRedo],
        [m.ILanguageService, language], [m.IDialogService, dialog], [m.INotificationService, notification],
        [m.ILanguageFeaturesService, features], [m.IAiEditTelemetryService, {}],
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
        return { binding, targetContext: { kind: 'node', connection: binding }, uriTransformer: new m.TargetUriTransformer(binding, () => []), runningLocation: { kind: 3 } };
      }
      async function connect(host) {
        const calls = [], documents = new Map(), renameRequests = [], renameDtos = [], commandResults = [], toMain = new m.Emitter(), toHost = new m.Emitter();
        const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)), drain: async () => {} }, null, host.uriTransformer);
        const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)), drain: async () => {} });
        const actualBulk = insta.createInstance(m.BulkEditService);
        // 只观察准入边界并原样转交；所有结果、资源统计、版本检查和写入都由原 BulkEditService 决定。
        const observedBulk = { apply(edits, options) { calls.push({ edits: edits.edits, options }); return actualBulk.apply(edits, options); } };
        const main = new m.MainThreadBulkEdits({ extensionHost: host }, observedBulk, log, identity);
        mainRpc.set(m.MainContext.MainThreadBulkEdits, main);
        const hostContext = { getProxy: hostRpc.getProxy.bind(hostRpc) };
        const documentOwner = new m.ExtHostDocumentsAndEditors(hostContext, log), textDocuments = new m.ExtHostDocuments(hostContext, documentOwner);
        const hostNotebooks = new m.ExtHostNotebookController(hostContext, { registerArgumentProcessor() {}, registerApiCommand() {} },
          documentOwner, textDocuments, undefined, undefined, log);
        const extHost = new m.ExtHostBulkEdits(hostContext, { getDocument: uri => documents.get(uri.toString()) }, hostNotebooks);
        const workspace = m.originalWorkspaceApi(extHost, { identifier: { value: 'probe.workspace-edits' } });
        const mainLanguage = new m.MainThreadLanguageFeatures({ extensionHost: host, getProxy: mainRpc.getProxy.bind(mainRpc) }, undefined, undefined, features,
          { asCanonicalUri: uri => uri }, insta, undefined);
        mainRpc.set(m.MainContext.MainThreadLanguageFeatures, mainLanguage);
        // 扩展回调与已冻结版本 DTO 是具名边界；上一片单独验证原 RenameAdapter 的版本 producer。
        hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, {
          async $provideRenameEdits(handle, resource, position, newName, token) {
            const request = { handle, resource: m.URI.revive(resource), position, newName, token }; renameRequests.push(request);
            const result = await client.onRename(request); renameDtos.push(result); return result;
          },
          async $supportsAutomaticNewSymbolNamesTriggerKind() { return false; },
        });
        const mainCommands = new m.MainThreadCommands({ extensionHost: host, getProxy: mainRpc.getProxy.bind(mainRpc) }, {
          async executeCommand(id, ...args) {
            const command = m.CommandsRegistry.getCommand(id);
            if (!command) throw new Error('未注册原命令：' + id);
            const source = m.unpackCommandSource(args);
            const result = await insta.invokeFunction(accessor => m.executeCommandForHost(command, accessor, source.args, source.source)); commandResults.push(result); return result;
          },
        }, extensions);
        mainRpc.set(m.MainContext.MainThreadCommands, mainCommands);
        const client = { host, calls, documents, renameRequests, renameDtos, commandResults, main, mainLanguage, actualBulk, workspace,
          syncNotebook(uri, versionId) {
            hostNotebooks.$acceptDocumentAndEditorsDelta(new m.SerializableObjectWithBuffers({ addedDocuments: [{ uri, versionId,
              viewType: 'probe', cells: [], metadata: {}, isDirty: false }] }));
          },
          remote: hostRpc.getProxy(m.MainContext.MainThreadBulkEdits), remoteCommands: hostRpc.getProxy(m.MainContext.MainThreadCommands),
          onRename: unexpected('Rename 扩展回调未配置'),
          async registerRename(handle = 1) {
            await hostRpc.getProxy(m.MainContext.MainThreadLanguageFeatures).$registerRenameSupport(handle, [{ $serialized: true, language: 'plaintext' }], false);
          },
          logical: uri => host.uriTransformer ? m.URI.revive(host.uriTransformer.transformIncoming(uri)) : uri,
          close() {
            hostNotebooks.$acceptDocumentAndEditorsDelta(new m.SerializableObjectWithBuffers({ removedDocuments: hostNotebooks.notebookDocuments.map(data => data.uri) }));
            textDocuments.dispose(); main.dispose(); mainLanguage.dispose(); mainCommands.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose();
          } };
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
      function editorFor(model) {
        const cursor = lifetime.add(new m.Emitter()), changeModel = lifetime.add(new m.Emitter()), registered = [];
        let selection = new m.Selection(1, 1, 1, 1);
        return { registered, hasModel: () => true, getModel: () => model, getPosition: () => selection.getPosition(), getSelection: () => selection,
          setSelection(value) { selection = m.Selection.liftSelection({ ...value, selectionStartLineNumber: value.startLineNumber, selectionStartColumn: value.startColumn,
            positionLineNumber: value.endLineNumber, positionColumn: value.endColumn }); }, focus() {},
          // 只读 editor 边界让原 BulkTextEdits 选择真实模型任务；不替换任何文本应用逻辑。
          getOption: () => true, onDidChangeCursorPosition: cursor.event, onDidChangeModel: changeModel.event, onDidChangeModelContent: model.onDidChangeContent.bind(model),
          invokeWithinContext(callback) { return callback({ get() { return { add(_editor, source) { registered.push(source); return () => {}; } }; } }); } };
      }
      function previewFor(client, boundary = {}) {
        const states = [], contexts = [], openContexts = [], inputs = [], tokens = [], requestTokens = [], restores = [], events = [];
        let contextEnabled = Boolean(boundary.oldSession);
        const pane = new m.BulkEditPane({ _sessionDisposables: lifetime.add(new m.DisposableStore()), _treeViewStates: new Map(),
          _treeDataSource: { groupByFile: true }, _ctxHasCategories: { set() {} }, _ctxHasCheckedChanges: { set() {} },
          _setState: state => states.push(state), _setTreeInput: input => { events.push(['tree', inputs.indexOf(input)]); boundary.entered?.resolve(); }, _tree: { updateChildren() {} },
          _instaService: {
            async invokeFunction(_create, edits) {
              const checked = lifetime.add(new m.Emitter()), input = { categories: [], checked: { checkedCount: edits.length, onDidChange: checked.event },
                getWorkspaceEdit: () => edits, disposed: 0, dispose() { this.disposed++; } }; inputs.push(input); events.push(['create', inputs.length - 1]);
              await boundary.create?.(); return input;
            },
            createInstance(_provider, input) { return { input, disposed: 0, dispose() { this.disposed++; } }; },
          } });
        const setInput = pane.setInput.bind(pane);
        pane.setInput = (edits, token) => { tokens.push(token); events.push(['setInput']); return setInput(edits, token); };
        if (boundary.hasInput) pane._currentInput = { getWorkspaceEdit: () => [] };
        const ui = { restore: async (...args) => { restores.push(args); await boundary.restore?.(...args); } };
        const owner = new m.OriginalPreviewContribution({ _ctxEnabled: { set(value) { contextEnabled = value; contexts.push(value); } }, _paneCompositeService: ui, _editorGroupsService: {},
          _viewsService: { async openView() {
            openContexts.push(contextEnabled); events.push(['open', contextEnabled]);
            // 原 view 注册以 refactorPreview.enabled 为 when；未启用时不能凭装具取得 Pane。
            if (!contextEnabled) return undefined;
            await boundary.open?.(); return pane;
          } },
          _dialogService: { async confirm() { events.push(['confirm']); await boundary.confirm?.(); return { confirmed: true }; } },
        }, { setPreviewHandler(handler) { return client.actualBulk.setPreviewHandler((edits, options) => { requestTokens.push(options.token); return handler(edits, options); }); } });
        if (boundary.oldSession) { const cts = lifetime.add(new m.CancellationTokenSource()); owner._activeSession = new m.PreviewSession(ui, cts); }
        return { pane, owner, states, contexts, openContexts, get contextEnabled() { return contextEnabled; }, inputs, tokens, requestTokens, restores, events };
      }
      async function providerFor(boundary = {}) {
        const child = lifetime.add(insta.createChild(new m.ServiceCollection(
          [m.IAccessibilityService, { alert: unexpected('预览语言检测通知') }],
          [m.ILanguageDetectionService, { isEnabledForLanguage: () => false }])));
        const actualResolver = lifetime.add(child.createInstance(m.TextModelResolverService)), sources = [], references = [], requests = [], registrations = [], queued = [], previewModels = [], editRequests = [];
        for (let index = 0; index < 2; index++) {
          const uri = m.URI.parse('inmemory:/provider-source-' + index + '.ts'), model = models.createModel('old-provider-' + index, null, uri);
          // 原 baseline 引用保住源模型；被测 Provider 另行请求并释放自己的原引用。
          lifetime.add(await actualResolver.createModelReference(uri)); sources.push(model);
        }
        const edits = sources.map(model => new m.ResourceTextEdit(model.uri, { range: new m.EditorRange(1, 1, 1, 4), text: 'preview' }));
        const input = lifetime.add(await child.invokeFunction(m.OriginalBulkFileOperations.create, edits)), getFileEdits = input.getFileEdits.bind(input);
        input.getFileEdits = async uri => { editRequests.push(uri.toString()); const result = await getFileEdits(uri); await boundary.edits?.(uri); return result; };
        lifetime.add(models.onModelAdded(model => { if (model.uri.scheme === 'vscode-bulkeditpreview-editor') previewModels.push(model); }));
        // 解析和释放运行原 TextModelResolverService；这里只观察原引用并注入回执延迟或错误。
        const resolver = {
          registerTextModelContentProvider(scheme, provider) {
            const registration = actualResolver.registerTextModelContentProvider(scheme, provider), observation = { scheme, disposed: 0 }; registrations.push(observation);
            return { dispose() { observation.disposed++; registration.dispose(); } };
          },
          async createModelReference(resource) {
            const kind = resource.scheme === 'inmemory' ? 'source' : 'preview'; requests.push({ kind, resource: resource.toString() });
            await boundary.beforeAcquire?.(kind, resource);
            const original = await actualResolver.createModelReference(resource), observation = { kind, resource: resource.toString(), returned: false, disposed: 0 };
            references.push(observation); await boundary.afterAcquire?.(kind, resource);
            observation.returned = true;
            return { object: original.object, dispose() { observation.disposed++; original.dispose(); } };
          },
        };
        const provider = lifetime.add(m.originalPreviewProvider(input, language, models, resolver, callback => queued.push(callback)));
        return { provider, actualResolver, sources, input, references, requests, registrations, queued, previewModels, editRequests,
          runQueued() { const callback = queued.shift(); if (!callback) throw new Error('原 Provider 未排入预期微任务'); return callback(); } };
      }
      const flush = () => new Promise(resolve => setTimeout(resolve, 0));
      globalThis.editProbe = { m, target, connect, openText, unloadedText, native, seed, read, dtoText, dtoCreate, defer, files, models, workingFiles, features, editorFor, previewFor, providerFor, flush,
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

test('真实 Notebook owner 生产的版本经公开 applyEdit 和 RPC 在首 file 前阻止过期整批编辑', async () => {
  const result = await inBrowser('Notebook版本生产到整批门禁', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A'));
    const native = p.native(a, 0, 'producer.ipynb'), resource = a.logical(native), created = p.native(a, 1, 'must-not-create.ts');
    a.syncNotebook(native, 3);
    p.notebooks.onResolve = async () => p.notebookVersion(resource, 4);
    const edit = new m.WorkspaceEdit(); edit.createFile(created, { contents: new TextEncoder().encode('bad') });
    edit.set(native, [m.NotebookEdit.updateNotebookMetadata({ changed: true })]);
    const applied = await a.workspace.applyEdit(edit);
    return { applied, exists: await p.files.exists(a.logical(created)), version: a.calls[0].edits[1].notebookVersionId,
      resource: a.calls[0].edits[1].resource.toString(), expectedResource: resource.toString(),
      notebookWrites: p.notebooks.writes, references: p.notebooks.references, requests: p.notebooks.requests,
      operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.version).toBe(3); expect(result.resource).toBe(result.expectedResource);
  expect(result.applied).toBe(false); expect(result.exists).toBe(false); expect(result.notebookWrites).toBe(0); expect(result.operations).toEqual([]);
  expect(result.requests).toEqual([result.expectedResource]); expect(result.references).toHaveLength(1);
  expect(result.references[0].disposed).toBe(true); expect(result.references[0].disposeCount).toBe(1);
  expect(result.warnings).toHaveLength(1); expect(result.warnings[0]).toContain('Notebook'); expect(result.warnings[0]).toContain('has changed in the meantime');
  expect(result.failures).toHaveLength(1);
  checks.push('原NotebookController的delta生成版本，经原公开applyEdit/WorkspaceEdit.from/RPC/URI转换/BulkEdit在首file前拒绝；目标Notebook resolver仍为具名边界');
});

test('同资源的文本与 Notebook 版本分别核对，重复 Notebook 后项冲突在首 file 前拒绝', async () => {
  const result = await inBrowser('Notebook已有版本字段整批门禁与重复资源', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'shared.ipynb'), resource = a.logical(native), created = p.native(a, 1, 'must-not-create.ts');
    const model = p.openText(a, native, 'old-text'), version = model.getVersionId(), notebook = p.notebookVersion(resource, version + 10);
    p.notebooks.onResolve = async () => notebook;
    // 此场景用显式 DTO 单独验证逐项多版本；真实版本生产经前一公开 API 场景验证。
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

for (const retirement of ['unregister', 'dispose']) test(`原 rawRename 已返回结果在 ${retirement} 后零应用，新注册和另一宿主仍成功`, async () => {
  const result = await inBrowser('Rename已返回结果的注册生命周期', async retirement => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const native = [p.native(a, 0, 'rename-first.ts'), p.native(a, 1, 'rename-second.ts')], created = p.native(a, 0, 'rename-created.ts');
    const models = native.map(uri => p.openText(a, uri, 'old-source')), peerNative = p.native(b, 0, 'peer.ts'), peer = p.openText(b, peerNative, 'old-peer');
    const response = request => ({ edits: [p.dtoCreate(created, request.newName), ...native.map((uri, index) => ({ ...p.dtoText(uri, request.newName), versionId: models[index].getVersionId() }))] });
    a.onRename = response; b.onRename = request => ({ edits: [{ ...p.dtoText(peerNative, request.newName), versionId: peer.getVersionId() }] });
    await a.registerRename(); await b.registerRename();
    const edit = await m.rawRename(p.features.renameProvider, models[0], new m.EditorPosition(1, 1), 'stale'), lifetime = edit.lifecycleToken;
    let previews = 0; p.lifetime.add(a.actualBulk.setPreviewHandler(async edits => { previews++; return edits; }));
    const descriptor = Object.getOwnPropertyDescriptor(edit, 'lifecycleToken');
    if (retirement === 'unregister') a.mainLanguage.$unregister(1); else a.mainLanguage.dispose();
    const stale = await a.actualBulk.apply(edit, { showPreview: true });
    const before = { values: models.map(model => model.getValue()), exists: await p.files.exists(a.logical(created)), operations: p.operations.length, resolutions: p.resolutions.length, previews };
    const freshClient = retirement === 'unregister' ? a : await p.connect(p.target('A')); freshClient.onRename = response; await freshClient.registerRename(2);
    const fresh = await m.rawRename(p.features.renameProvider, models[0], new m.EditorPosition(1, 1), 'fresh');
    const freshApplied = await freshClient.actualBulk.apply(fresh), peerEdit = await m.rawRename(p.features.renameProvider, peer, new m.EditorPosition(1, 1), 'peer');
    return { stale, before, cancelled: lifetime.isCancellationRequested, enumerable: descriptor.enumerable, dtoHasToken: a.renameDtos.some(dto => Object.hasOwn(dto, 'lifecycleToken')),
      freshApplied, peerApplied: await b.actualBulk.apply(peerEdit), freshToken: fresh.lifecycleToken !== lifetime && !fresh.lifecycleToken.isCancellationRequested,
      values: models.map(model => model.getValue()), peer: peer.getValue(), created: await p.read(a.logical(created)), warnings: p.warnings, failures: p.failures };
  }, retirement);
  expect(result.stale.isApplied).toBe(false); expect(result.before).toEqual({ values: ['old-source', 'old-source'], exists: false, operations: 0, resolutions: 0, previews: 0 });
  expect(result.cancelled).toBe(true); expect(result.enumerable).toBe(false); expect(result.dtoHasToken).toBe(false); expect(result.freshApplied.isApplied).toBe(true);
  expect(result.peerApplied.isApplied).toBe(true); expect(result.freshToken).toBe(true); expect(result.values).toEqual(['fresh-source', 'fresh-source']); expect(result.peer).toBe('peer-peer');
  expect(result.created).toBe('fresh'); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push(`原rawRename结果在${retirement}后不进预览/解析/文件/文本；新注册和B正常`);
});

test('原公开 Rename 命令经 MainThreadCommands 和真实 RPC 返回时不序列化窗口生命周期', async () => {
  const result = await inBrowser('Rename窗口结果与公开命令RPC隔离', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'command.ts'), model = p.openText(a, native, 'old-command');
    a.onRename = request => ({ edits: [{ ...p.dtoText(native, request.newName), versionId: model.getVersionId() }] }); await a.registerRename();
    const remote = await a.remoteCommands.$executeCommand('_executeDocumentRenameProvider', [native, new m.EditorPosition(1, 1), 'rpc'], false);
    const windowResult = a.commandResults[0], token = windowResult.lifecycleToken;
    const before = { hasWindowToken: Boolean(token), enumerable: Object.getOwnPropertyDescriptor(windowResult, 'lifecycleToken').enumerable,
      remoteHasToken: Object.hasOwn(remote, 'lifecycleToken'), jsonHasToken: JSON.stringify(remote).includes('lifecycleToken'),
      windowJsonHasToken: JSON.stringify(windowResult).includes('lifecycleToken'), remoteText: remote.edits[0].textEdit.text,
      resource: m.URI.revive(remote.edits[0].resource).toString(), expected: native.toString() };
    a.mainLanguage.$unregister(1);
    return { before, sameToken: windowResult.lifecycleToken === token, cancelled: token.isCancellationRequested,
      applied: await a.actualBulk.apply(windowResult), value: model.getValue(), operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.before).toEqual({ hasWindowToken: true, enumerable: false, remoteHasToken: false, jsonHasToken: false, windowJsonHasToken: false,
    remoteText: 'rpc', resource: result.before.expected, expected: result.before.expected });
  expect(result.sameToken).toBe(true); expect(result.cancelled).toBe(true); expect(result.applied.isApplied).toBe(false); expect(result.value).toBe('old-command');
  expect(result.operations).toEqual([]); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push('真实原Rename命令/MainThreadCommands/RPC只返回edit DTO，窗口原对象的不可枚举token仍随注册取消');
});

for (const stage of ['open', 'confirm', 'restore', 'create', 'input']) for (const cancellation of ['registration', 'request']) {
  test(`原预览 owner 在 ${stage} 等待中由 ${cancellation} 取消，首文件与双根文本零写入`, async () => {
    const result = await inBrowser('Rename真实预览异步取消', async ({ stage, cancellation }) => {
      const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), gate = p.defer(), source = p.lifetime.add(new m.CancellationTokenSource());
      const native = [p.native(a, 0, 'preview-first.ts'), p.native(a, 1, 'preview-second.ts')], created = p.native(a, 0, 'preview-created.ts');
      const models = native.map(uri => p.openText(a, uri, 'old-preview'));
      a.onRename = () => ({ edits: [p.dtoCreate(created), ...native.map((uri, index) => ({ ...p.dtoText(uri, 'bad'), versionId: models[index].getVersionId() }))] }); await a.registerRename();
      const edit = await m.rawRename(p.features.renameProvider, models[0], new m.EditorPosition(1, 1), 'bad');
      const boundary = { hasInput: stage === 'confirm', oldSession: stage === 'restore' };
      if (stage === 'input') boundary.entered = entered;
      else boundary[stage] = async (...args) => { if (stage !== 'restore' || args[0] === false) { entered.resolve(); await gate.promise; } };
      const preview = p.previewFor(a, boundary), oldSession = preview.owner._activeSession;
      let settled = false;
      const pending = a.actualBulk.apply(edit, { showPreview: true, token: source.token }).then(value => { settled = true; return value; });
      await Promise.race([entered.promise, pending.then(() => { throw new Error('未经过指定原预览等待点：' + stage); })]);
      const before = { values: models.map(model => model.getValue()), exists: await p.files.exists(a.logical(created)), operations: p.operations.length, settled,
        openContexts: [...preview.openContexts], contextEnabled: preview.contextEnabled };
      if (cancellation === 'registration') a.mainLanguage.$unregister(1); else source.cancel();
      const previewCancelled = preview.tokens.map(token => token.isCancellationRequested); gate.resolve(); const applied = await pending;
      return { before, applied, previewCancelled, lifecycleCancelled: edit.lifecycleToken.isCancellationRequested, requestCancelled: source.token.isCancellationRequested,
        values: models.map(model => model.getValue()), exists: await p.files.exists(a.logical(created)), operations: p.operations, resolutions: p.resolutions.length,
        treeInputs: preview.events.filter(event => event[0] === 'tree').length, inputs: preview.inputs.map(input => input.disposed), currentInput: Boolean(preview.pane._currentInput),
        currentResolve: Boolean(preview.pane._currentResolve), contexts: preview.contexts, contextEnabled: preview.contextEnabled, restores: preview.restores,
        activeSession: oldSession && preview.owner._activeSession === oldSession ? 'previous' : preview.owner._activeSession ? 'new' : 'none', warnings: p.warnings, failures: p.failures };
    }, { stage, cancellation });
    expect(result.before).toEqual({ values: ['old-preview', 'old-preview'], exists: false, operations: 0, settled: false, openContexts: [true], contextEnabled: true }); expect(result.applied.isApplied).toBe(false);
    expect(result.lifecycleCancelled).toBe(cancellation === 'registration'); expect(result.requestCancelled).toBe(cancellation === 'request'); expect(result.values).toEqual(['old-preview', 'old-preview']);
    expect(result.exists).toBe(false); expect(result.operations).toEqual([]); expect(result.resolutions).toBe(0);
    expect(result.treeInputs).toBe(stage === 'input' ? 1 : 0); expect(result.inputs).toEqual(['create', 'input'].includes(stage) ? [1] : []);
    expect(result.previewCancelled).toEqual(['create', 'input'].includes(stage) ? [true] : []); expect(result.currentResolve).toBe(false);
    expect(result.contexts).toEqual(stage === 'restore' ? [true] : [true, false]); expect(result.contextEnabled).toBe(stage === 'restore');
    expect(result.activeSession).toBe(stage === 'restore' ? 'previous' : 'none'); expect(result.currentInput).toBe(stage === 'confirm');
    expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push(`原preview在getPane前启用view上下文，handler/session/Pane.setInput在${stage}由${cancellation}取消，保留旧owner身份且零业务写入`);
  });
}

test('Rename 原文件创建已完成而 did 回执等待时撤代，保留创建成功并跳过后续双根文本', async () => {
  const result = await inBrowser('Rename部分完成后的诚实结果', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), gate = p.defer();
    const native = [p.native(a, 0, 'partial-first.ts'), p.native(a, 1, 'partial-second.ts')], created = p.native(a, 0, 'partial-created.ts');
    const models = native.map(uri => p.openText(a, uri, 'old-partial'));
    a.onRename = () => ({ edits: [p.dtoCreate(created, 'completed-file'), ...native.map((uri, index) => ({ ...p.dtoText(uri, 'bad'), versionId: models[index].getVersionId() }))] }); await a.registerRename();
    const edit = await m.rawRename(p.features.renameProvider, models[0], new m.EditorPosition(1, 1), 'bad');
    p.lifetime.add(p.workingFiles.onDidRunWorkingCopyFileOperation(event => {
      if (event.operation === m.FileOperation.CREATE && event.files.some(file => file.target.toString() === a.logical(created).toString())) { event.waitUntil(gate.promise); entered.resolve(); }
    }));
    const pending = a.actualBulk.apply(edit); await Promise.race([entered.promise, pending.then(() => { throw new Error('未经过原文件完成事件'); })]);
    const before = { content: await p.read(a.logical(created)), values: models.map(model => model.getValue()), references: p.resolutions.length, held: p.resolutions.every(ref => !ref.disposed) };
    a.mainLanguage.$unregister(1); gate.resolve(); const applied = await pending;
    return { before, applied, values: models.map(model => model.getValue()), content: await p.read(a.logical(created)), cancelled: edit.lifecycleToken.isCancellationRequested,
      released: p.resolutions.every(ref => ref.disposed && ref.disposeCount === 1), operations: p.operations.map(event => event.phase), warnings: p.warnings, failures: p.failures };
  });
  expect(result.before).toEqual({ content: 'completed-file', values: ['old-partial', 'old-partial'], references: 2, held: true }); expect(result.applied.isApplied).toBe(true);
  expect(result.cancelled).toBe(true); expect(result.values).toEqual(['old-partial', 'old-partial']); expect(result.content).toBe('completed-file'); expect(result.operations).toEqual(['will', 'did']);
  expect(result.released).toBe(true); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push('原Rename结果首create真实完成后撤代，保留已完成成功、后续文本不写且预检引用释放');
});

test('成功应用回收生命周期和父 token 订阅，Web 结果与原数组继续保留应用语义', async () => {
  const result = await inBrowser('Rename完成释放与Web/数组对照', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), source = p.lifetime.add(new m.CancellationTokenSource()), native = p.native(a, 0, 'success.ts');
    const model = p.openText(a, native, 'old-success'); a.onRename = request => ({ edits: [{ ...p.dtoText(native, request.newName), versionId: model.getVersionId() }] }); await a.registerRename();
    const edit = await m.rawRename(p.features.renameProvider, model, new m.EditorPosition(1, 1), 'new'), entered = p.defer(), preview = p.previewFor(a, { entered });
    const pending = a.actualBulk.apply(edit, { showPreview: true, token: source.token });
    await Promise.race([entered.promise, pending.then(() => { throw new Error('未经过真实预览输入'); })]);
    const sessionToken = preview.tokens[0], mergedToken = preview.requestTokens[0];
    preview.pane._done(true); const applied = await pending;
    a.mainLanguage.$unregister(1); source.cancel();
    const web = await p.connect({ runningLocation: { kind: 2 } }), webNative = m.URI.file('/web-result.ts'), webModel = p.openText(web, webNative, 'old-web');
    web.onRename = request => ({ edits: [{ ...p.dtoText(webNative, request.newName), versionId: webModel.getVersionId() }] }); await web.registerRename();
    const webEdit = await m.rawRename(p.features.renameProvider, webModel, new m.EditorPosition(1, 1), 'web'); web.mainLanguage.dispose();
    const webApplied = await web.actualBulk.apply(webEdit), arrayApplied = await web.actualBulk.apply([new m.ResourceTextEdit(webNative, { range: new m.EditorRange(1, 1, 1, 4), text: 'array' })]);
    return { applied, cancelled: edit.lifecycleToken.isCancellationRequested, parentCancelled: source.token.isCancellationRequested,
      completedTokensCancelled: [sessionToken.isCancellationRequested, mergedToken.isCancellationRequested], value: model.getValue(), inputDisposed: preview.inputs.map(input => input.disposed),
      webHasToken: Object.hasOwn(webEdit, 'lifecycleToken'), webApplied, arrayApplied, webValue: webModel.getValue(), warnings: p.warnings, failures: p.failures };
  });
  expect(result.applied.isApplied).toBe(true); expect(result.cancelled).toBe(true); expect(result.parentCancelled).toBe(true); expect(result.completedTokensCancelled).toEqual([false, false]);
  expect(result.value).toBe('new-success'); expect(result.inputDisposed).toEqual([1]); expect(result.webHasToken).toBe(false); expect(result.webApplied.isApplied).toBe(true);
  expect(result.arrayApplied.isApplied).toBe(true); expect(result.webValue).toBe('array-web'); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push('真实预览成功后CTS链已释放，原结果/父取消不再联动完成token；Web返回和数组仍正常应用');
});

test('原 RenameController.run 等待真实应用完成，自身文本变化只取消状态 token', async () => {
  const result = await inBrowser('Controller父token及应用完成顺序', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), gate = p.defer(), alerts = [], notices = [], applications = [];
    const native = [p.native(a, 0, 'controller-first.ts'), p.native(a, 1, 'controller-second.ts')], created = p.native(a, 0, 'controller-created.ts');
    const models = native.map(uri => p.openText(a, uri, 'old-controller')), editor = p.editorFor(models[0]);
    a.onRename = () => ({ edits: [{ ...p.dtoText(native[0], 'new'), versionId: models[0].getVersionId() }, p.dtoCreate(created, 'controller-file'),
      { ...p.dtoText(native[1], 'new'), versionId: models[1].getVersionId() }] }); await a.registerRename();
    p.lifetime.add(p.workingFiles.onDidRunWorkingCopyFileOperation(event => {
      if (event.operation === m.FileOperation.CREATE && event.files.some(file => file.target.toString() === a.logical(created).toString())) { event.waitUntil(gate.promise); entered.resolve(); }
    }));
    const controller = m.originalRenameController({ editor, _cts: p.lifetime.add(new m.CancellationTokenSource()), _languageFeaturesService: p.features,
      _logService: new m.NullLogService(), _progressService: { showWhile() {} }, _configService: { getValue: () => false },
      _notificationService: { info: value => notices.push(value), error: value => notices.push(value) },
      _renameWidget: { async getInput() { return { newName: 'new', wantsPreview: false }; } },
      _bulkEditService: { hasPreviewHandler: () => false, apply(edit, options) { applications.push({ edit, options }); return a.actualBulk.apply(edit, options); } },
    }, value => alerts.push(value));
    let settled = false; const pending = controller.run().then(() => { settled = true; });
    await Promise.race([entered.promise, pending.then(() => { throw new Error('Controller在真实应用完成前退出'); })]);
    const before = { settled, values: models.map(model => model.getValue()), created: await p.read(a.logical(created)), alerts: alerts.length,
      parentToken: applications[0].options.token === controller._cts.token, stateToken: applications[0].options.token === editor.registered[1].token,
      stateCancelled: editor.registered[1].token.isCancellationRequested, operationCancelled: applications[0].options.token.isCancellationRequested };
    gate.resolve(); await pending;
    return { before, values: models.map(model => model.getValue()), settled, alerts, notices, applications: applications.length,
      released: p.resolutions.every(ref => ref.disposed && ref.disposeCount === 1), warnings: p.warnings, failures: p.failures };
  });
  expect(result.before).toEqual({ settled: false, values: ['new-controller', 'old-controller'], created: 'controller-file', alerts: 0,
    parentToken: true, stateToken: false, stateCancelled: true, operationCancelled: false });
  expect(result.settled).toBe(true); expect(result.values).toEqual(['new-controller', 'new-controller']); expect(result.alerts).toHaveLength(1);
  expect(result.notices).toEqual([]); expect(result.applications).toBe(1); expect(result.released).toBe(true); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push('AST原Controller.run/原状态CTS经RPC和真实BulkEdit：首文本改变取消cts2，冻结父token保持、run/aria等待后续file/text完成');
});

test('原 RenameController 的父操作取消关闭真实预览，不播报未应用的成功摘要', async () => {
  const result = await inBrowser('Controller取消时真实preview清理和aria准入', async () => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), alerts = [], notices = [], applications = [];
    const native = p.native(a, 0, 'controller-cancel.ts'), model = p.openText(a, native, 'old-cancel'), created = p.native(a, 1, 'controller-cancel-created.ts'), editor = p.editorFor(model);
    a.onRename = () => ({ edits: [p.dtoCreate(created), { ...p.dtoText(native, 'bad'), versionId: model.getVersionId() }] }); await a.registerRename();
    const preview = p.previewFor(a, { entered });
    const controller = m.originalRenameController({ editor, _cts: p.lifetime.add(new m.CancellationTokenSource()), _languageFeaturesService: p.features,
      _logService: new m.NullLogService(), _progressService: { showWhile() {} }, _configService: { getValue: () => true },
      _notificationService: { info: value => notices.push(value), error: value => notices.push(value) },
      _renameWidget: { async getInput() { return { newName: 'bad', wantsPreview: true }; } },
      _bulkEditService: { hasPreviewHandler: () => true, async apply(edit, options) { const result = await a.actualBulk.apply(edit, options); applications.push(result); return result; } },
    }, value => alerts.push(value));
    let settled = false; const pending = controller.run().then(() => { settled = true; });
    await Promise.race([entered.promise, pending.then(() => { throw new Error('未经过Controller原预览'); })]);
    const pendingBeforeCancel = !settled; controller._cts.dispose(true); await pending;
    return { pendingBeforeCancel, settled, alerts, notices, applications, value: model.getValue(), exists: await p.files.exists(a.logical(created)),
      previewCancelled: preview.tokens[0].isCancellationRequested, inputDisposed: preview.inputs.map(input => input.disposed), contexts: preview.contexts,
      operations: p.operations, warnings: p.warnings, failures: p.failures };
  });
  expect(result.pendingBeforeCancel).toBe(true); expect(result.settled).toBe(true); expect(result.applications).toHaveLength(1); expect(result.applications[0].isApplied).toBe(false);
  expect(result.alerts).toEqual([]); expect(result.notices).toEqual([]); expect(result.value).toBe('old-cancel'); expect(result.exists).toBe(false);
  expect(result.previewCancelled).toBe(true); expect(result.inputDisposed).toEqual([1]); expect(result.contexts).toEqual([true, false]); expect(result.operations).toEqual([]);
  expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push('AST原Controller父操作取消传入真实preview，零file/text且isApplied=false禁止成功aria');
});

for (const cancellation of ['registration', 'runnable']) test(`原 inline 缓存结果由 ${cancellation} 取消后 count=0，真实命令零 apply`, async () => {
  const result = await inBrowser('Inline缓存退役结果准入', async cancellation => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), native = p.native(a, 0, 'inline-stale.ts'), model = p.openText(a, native, 'old-inline');
    a.onRename = () => ({ edits: [{ ...p.dtoText(native, 'bad'), versionId: model.getVersionId() }] }); await a.registerRename();
    const editor = p.editorFor(model), codeEditors = { getFocusedCodeEditor: () => editor }, commandService = { executeCommand() { throw new Error('本片不调用NES命令'); } }, applications = [];
    const runnable = new m.RenameSymbolRunnable(p.features, commandService, 'inline-stale', model, m.InlineEditorState.create(codeEditors, model), new m.EditorPosition(1, 1), 'bad');
    const countBefore = await runnable.getCount(), cached = await runnable.getWorkspaceEdit();
    const processor = p.lifetime.add(new m.RenameSymbolProcessor(commandService, p.features, undefined, { apply(...args) { applications.push(args); throw new Error('取消的inline不应apply'); } }, undefined, codeEditors));
    processor._renameRunnable = runnable;
    if (cancellation === 'registration') a.mainLanguage.$unregister(1); else runnable.cancel();
    await m.CommandsRegistry.getCommand(m.renameSymbolCommandId).handler(undefined, m.EditSources.rename('old', 'bad'), runnable);
    return { countBefore, countAfter: await runnable.getCount(), editAfter: await runnable.getWorkspaceEdit() === undefined, cachedCancelled: cached.lifecycleToken.isCancellationRequested,
      runnableCancelled: runnable.token.isCancellationRequested, cleared: processor._renameRunnable === undefined, applications: applications.length, requests: a.renameRequests.length,
      value: model.getValue(), operations: p.operations, resolutions: p.resolutions.length, warnings: p.warnings, failures: p.failures };
  }, cancellation);
  expect(result.countBefore).toBe(1); expect(result.countAfter).toBe(0); expect(result.editAfter).toBe(true); expect(result.cachedCancelled).toBe(cancellation === 'registration');
  expect(result.runnableCancelled).toBe(cancellation === 'runnable'); expect(result.cleared).toBe(true); expect(result.applications).toBe(0); expect(result.requests).toBe(1);
  expect(result.value).toBe('old-inline'); expect(result.operations).toEqual([]); expect(result.resolutions).toBe(0); expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]);
  checks.push(`AST原runnable缓存经${cancellation}取消后由原getResult过滤，原Processor命令零apply并清除缓存`);
});

for (const cancel of [false, true]) test(`原 inline 命令等待真实预览${cancel ? '取消' : '确认'}，finally 才释放 runnable`, async () => {
  const result = await inBrowser('Inline命令应用等待及token消费', async cancel => {
    const p = globalThis.editProbe, m = p.m, a = await p.connect(p.target('A')), entered = p.defer(), native = p.native(a, 0, 'inline-command.ts'), model = p.openText(a, native, 'old-inline');
    const created = p.native(a, 1, 'inline-created.ts');
    a.onRename = () => ({ edits: [{ ...p.dtoCreate(created), metadata: { needsConfirmation: true, label: 'inline预览' } }, { ...p.dtoText(native, 'new'), versionId: model.getVersionId() }] }); await a.registerRename();
    const editor = p.editorFor(model), codeEditors = { getFocusedCodeEditor: () => editor }, commandService = { executeCommand() { throw new Error('本片不调用NES命令'); } }, applications = [];
    const runnable = new m.RenameSymbolRunnable(p.features, commandService, 'inline-command', model, m.InlineEditorState.create(codeEditors, model), new m.EditorPosition(1, 1), 'new');
    const countBefore = await runnable.getCount(), preview = p.previewFor(a, { entered }), source = m.EditSources.rename('old', 'new');
    const processor = p.lifetime.add(new m.RenameSymbolProcessor(commandService, p.features, undefined, {
      async apply(edit, options) { const observation = { token: options.token === runnable.token, reason: options.reason === source }; applications.push(observation);
        const result = await a.actualBulk.apply(edit, options); observation.isApplied = result.isApplied; return result; },
    }, undefined, codeEditors));
    processor._renameRunnable = runnable;
    let settled = false; const pending = m.CommandsRegistry.getCommand(m.renameSymbolCommandId).handler(undefined, source, runnable).then(() => { settled = true; });
    await Promise.race([entered.promise, pending.then(() => { throw new Error('inline命令在真实apply完成前已退出'); })]);
    const before = { settled, retained: processor._renameRunnable === runnable, value: model.getValue(), exists: await p.files.exists(a.logical(created)) };
    if (cancel) runnable.cancel(); else preview.pane._done(true);
    await pending;
    return { countBefore, before, settled, cleared: processor._renameRunnable === undefined, applications, value: model.getValue(), exists: await p.files.exists(a.logical(created)),
      inputDisposed: preview.inputs.map(input => input.disposed), previewCancelled: preview.tokens[0].isCancellationRequested, countAfter: await runnable.getCount(), warnings: p.warnings, failures: p.failures };
  }, cancel);
  expect(result.countBefore).toBe(2); expect(result.before).toEqual({ settled: false, retained: true, value: 'old-inline', exists: false }); expect(result.settled).toBe(true);
  expect(result.cleared).toBe(true); expect(result.applications).toEqual([{ token: true, reason: true, isApplied: !cancel }]); expect(result.value).toBe(cancel ? 'old-inline' : 'new-inline');
  expect(result.exists).toBe(!cancel); expect(result.inputDisposed).toEqual([1]); expect(result.previewCancelled).toBe(cancel); expect(result.countAfter).toBe(cancel ? 0 : 2);
  expect(result.warnings).toEqual([]); expect(result.failures).toEqual([]); checks.push(`原Processor命令传runnable.token、等待真实preview${cancel ? '取消' : '确认'}及BulkEdit完成后finally清缓存`);
});

test('原预览Provider在源引用回执等待中退役，释放迟到引用且停止后续模型和初始化', async () => {
  const result = await inBrowser('Provider源引用等待中退役', async () => {
    const p = globalThis.editProbe, m = p.m, entered = p.defer(), gate = p.defer();
    const fixture = await p.providerFor({ async afterAcquire(kind) { if (kind === 'source') { entered.resolve(); await gate.promise; } } });
    const provider = fixture.provider, previewUri = provider.asPreviewUri(fixture.sources[0].uri);
    const pendingContent = provider.provideTextContent(previewUri).then(() => ({ resolved: true }), error => ({ resolved: false, cancelled: m.isCancellationError(error) }));
    await Promise.race([entered.promise, provider._ready.then(() => { throw new Error('未经过原Provider源引用回执等待'); })]);
    const before = { previews: fixture.previewModels.length, requests: fixture.requests.map(item => item.kind), references: fixture.references.map(item => [item.kind, item.returned, item.disposed]) };
    provider.dispose(); gate.resolve(); await provider._ready; const content = await pendingContent;
    const retiredEmpty = await provider.provideTextContent(provider.constructor.emptyPreview).then(() => false, error => m.isCancellationError(error));
    await p.flush();
    return { before, content, retiredEmpty, previews: fixture.previewModels.length, queued: fixture.queued.length, requests: fixture.requests.map(item => item.kind),
      references: fixture.references.map(item => [item.kind, item.returned, item.disposed]), registrations: fixture.registrations.map(item => item.disposed), editRequests: fixture.editRequests,
      sourceValues: fixture.sources.map(model => model.getValue()), handlesScheme: fixture.actualResolver.canHandleResource(previewUri), failures: p.failures };
  });
  expect(result.before).toEqual({ previews: 0, requests: ['source'], references: [['source', false, 0]] }); expect(result.content).toEqual({ resolved: false, cancelled: true });
  expect(result.retiredEmpty).toBe(true); expect(result.previews).toBe(0); expect(result.queued).toBe(0); expect(result.requests).toEqual(['source']);
  expect(result.references).toEqual([['source', true, 1]]); expect(result.registrations).toEqual([1]); expect(result.editRequests).toEqual([]);
  expect(result.sourceValues).toEqual(['old-provider-0', 'old-provider-1']); expect(result.handlesScheme).toBe(false); expect(result.failures).toEqual([]);
  checks.push('AST原Provider源ref await后退役立即释放原resolver引用，不建preview/第二源/队列，provideTextContent含empty明确取消');
});

test('原预览Provider在编辑计算等待中退役，不向迟到模型写入且队列释放未引用模型', async () => {
  const result = await inBrowser('Provider编辑等待中退役', async () => {
    const p = globalThis.editProbe, entered = p.defer(), gate = p.defer();
    const fixture = await p.providerFor({ async edits() { entered.resolve(); await gate.promise; } });
    await Promise.race([entered.promise, fixture.provider._ready.then(() => { throw new Error('未经过原Provider编辑计算等待'); })]);
    const preview = fixture.previewModels[0], before = { previews: fixture.previewModels.length, value: preview.getValue(), queued: fixture.queued.length, edits: fixture.editRequests.length };
    fixture.provider.dispose(); gate.resolve(); await fixture.provider._ready;
    const beforeQueue = { value: preview.getValue(), edits: fixture.editRequests.length };
    await fixture.runQueued(); await p.flush();
    return { before, beforeQueue, disposed: preview.isDisposed(), registeredModel: Boolean(p.models.getModel(preview.uri)), previews: fixture.previewModels.length,
      queued: fixture.queued.length, references: fixture.references.map(item => [item.kind, item.disposed]), requests: fixture.requests.map(item => item.kind),
      sourceValues: fixture.sources.map(model => model.getValue()), failures: p.failures };
  });
  expect(result.before).toEqual({ previews: 1, value: 'old-provider-0', queued: 1, edits: 1 }); expect(result.beforeQueue).toEqual({ value: 'old-provider-0', edits: 1 });
  expect(result.disposed).toBe(true); expect(result.registeredModel).toBe(false); expect(result.previews).toBe(1); expect(result.queued).toBe(0);
  expect(result.references).toEqual([['source', 1]]); expect(result.requests).toEqual(['source']); expect(result.sourceValues).toEqual(['old-provider-0', 'old-provider-1']); expect(result.failures).toEqual([]);
  checks.push('原Provider await edits后退役不apply迟到编辑、不继续第二源；执行原queued callback释放尚无resolver引用的原preview模型');
});

test('原预览Provider微任务执行前退役，零新增引用且已建预览模型全部释放', async () => {
  const result = await inBrowser('Provider已排队引用在退役后停止', async () => {
    const p = globalThis.editProbe, fixture = await p.providerFor(); await fixture.provider._ready;
    const previews = [...fixture.previewModels], previewUri = fixture.provider.asPreviewUri(fixture.sources[0].uri);
    const provided = await fixture.provider.provideTextContent(previewUri);
    const before = { count: previews.length, values: previews.map(model => model.getValue()), queued: fixture.queued.length, identity: provided === previews[0], requests: fixture.requests.map(item => item.kind) };
    fixture.provider.dispose(); while (fixture.queued.length) await fixture.runQueued(); await p.flush();
    return { before, requests: fixture.requests.map(item => item.kind), disposed: previews.map(model => model.isDisposed()), registeredModels: previews.map(model => Boolean(p.models.getModel(model.uri))),
      registrations: fixture.registrations.map(item => item.disposed), references: fixture.references.map(item => [item.kind, item.disposed]),
      sources: fixture.sources.map(model => model.getValue()), handlesScheme: fixture.actualResolver.canHandleResource(previewUri), failures: p.failures };
  });
  expect(result.before).toEqual({ count: 2, values: ['preview-provider-0', 'preview-provider-1'], queued: 2, identity: true, requests: ['source', 'source'] });
  expect(result.requests).toEqual(['source', 'source']); expect(result.disposed).toEqual([true, true]); expect(result.registeredModels).toEqual([false, false]);
  expect(result.registrations).toEqual([1]); expect(result.references).toEqual([['source', 1], ['source', 1]]); expect(result.sources).toEqual(['old-provider-0', 'old-provider-1']);
  expect(result.handlesScheme).toBe(false); expect(result.failures).toEqual([]); checks.push('原queued callback在store退役后不请求已注销scheme，并释放本owner已创建而尚未引用的两个真实preview模型');
});

for (const outcome of ['resolve', 'reject']) test(`原预览Provider微任务引用在途时退役，迟到 ${outcome} 释放原模型且零告警`, async () => {
  const result = await inBrowser('Provider在途预览引用退役结算', async outcome => {
    const p = globalThis.editProbe, m = p.m, entered = p.defer(), gate = p.defer(), reported = [], error = new Error('退役后原预览引用失败');
    const previous = m.errorHandler.getUnexpectedErrorHandler(); m.setUnexpectedErrorHandler(failure => reported.push(failure));
    try {
      const boundary = outcome === 'resolve' ? { async afterAcquire(kind) { if (kind === 'preview') { entered.resolve(); await gate.promise; } } }
        : { async beforeAcquire(kind) { if (kind === 'preview') { entered.resolve(); await gate.promise; throw error; } } };
      const fixture = await p.providerFor(boundary); await fixture.provider._ready;
      const previews = [...fixture.previewModels]; await fixture.runQueued();
      await entered.promise;
      const before = { requests: fixture.requests.map(item => item.kind), previewRefs: fixture.references.filter(item => item.kind === 'preview').map(item => [item.returned, item.disposed]),
        disposed: previews.map(model => model.isDisposed()) };
      fixture.provider.dispose(); gate.resolve(); await p.flush(); while (fixture.queued.length) await fixture.runQueued(); await p.flush();
      return { before, requests: fixture.requests.map(item => item.kind), references: fixture.references.map(item => [item.kind, item.returned, item.disposed]),
        disposed: previews.map(model => model.isDisposed()), registeredModels: previews.map(model => Boolean(p.models.getModel(model.uri))), reports: reported.map(item => item.message),
        registrations: fixture.registrations.map(item => item.disposed), sources: fixture.sources.map(model => model.getValue()), failures: p.failures };
    } finally { m.setUnexpectedErrorHandler(previous); }
  }, outcome);
  expect(result.before).toEqual({ requests: ['source', 'source', 'preview'], previewRefs: outcome === 'resolve' ? [[false, 0]] : [], disposed: [false, false] });
  expect(result.requests).toEqual(['source', 'source', 'preview']); expect(result.references).toEqual([['source', true, 1], ['source', true, 1], ...(outcome === 'resolve' ? [['preview', true, 1]] : [])]);
  expect(result.disposed).toEqual([true, true]); expect(result.registeredModels).toEqual([false, false]); expect(result.reports).toEqual([]); expect(result.registrations).toEqual([1]);
  expect(result.sources).toEqual(['old-provider-0', 'old-provider-1']); expect(result.failures).toEqual([]); checks.push(`原queued ref在途后退役再${outcome}，迟到原引用只释放一次、失败明确归退役且真实模型释放，无全局错误`);
});

test('原预览Provider活跃时微任务引用失败，原 onUnexpectedError 报告同一错误并释放模型', async () => {
  const result = await inBrowser('Provider活跃引用错误归属', async () => {
    const p = globalThis.editProbe, m = p.m, failure = new Error('fresh-preview-reference-failed'), reports = [], events = [], reported = p.defer();
    const previous = m.errorHandler.getUnexpectedErrorHandler(), remove = m.errorHandler.addListener(error => events.push(error));
    m.setUnexpectedErrorHandler(error => { reports.push(error); reported.resolve(); });
    try {
      const fixture = await p.providerFor({ async beforeAcquire(kind) { if (kind === 'preview') throw failure; } }); await fixture.provider._ready;
      const previews = [...fixture.previewModels]; await fixture.runQueued(); await reported.promise;
      const active = { sameError: reports[0] === failure, sameEvent: events[0] === failure, reports: reports.length, events: events.length,
        firstDisposed: previews[0].isDisposed(), firstRemoved: !p.models.getModel(previews[0].uri), secondAlive: !previews[1].isDisposed(),
        schemeRegistered: fixture.actualResolver.canHandleResource(previews[1].uri), references: fixture.references.map(item => [item.kind, item.disposed]) };
      fixture.provider.dispose(); while (fixture.queued.length) await fixture.runQueued(); await p.flush();
      return { active, requests: fixture.requests.map(item => item.kind), disposed: previews.map(model => model.isDisposed()), reports: reports.length, events: events.length,
        sources: fixture.sources.map(model => model.getValue()), failures: p.failures };
    } finally { remove(); m.setUnexpectedErrorHandler(previous); }
  });
  expect(result.active).toEqual({ sameError: true, sameEvent: true, reports: 1, events: 1, firstDisposed: true, firstRemoved: true, secondAlive: true,
    schemeRegistered: true, references: [['source', 1], ['source', 1]] }); expect(result.requests).toEqual(['source', 'source', 'preview']); expect(result.disposed).toEqual([true, true]);
  expect(result.reports).toBe(1); expect(result.events).toBe(1); expect(result.sources).toEqual(['old-provider-0', 'old-provider-1']); expect(result.failures).toEqual([]);
  checks.push('原Provider活跃queued ref失败经原onUnexpectedError报告同一个Error/事件一次，失败模型释放且另一模型/源不被误伤');
});

for (const stage of ['resources', 'resourceId']) for (const changed of ['input', 'provider']) test(`原Pane旧点击在 ${stage} 等待后 ${changed} 已替换，不打开迟到 multidiff`, async () => {
  const result = await inBrowser('Pane异步旧点击按输入和Provider身份拒绝', async ({ stage, changed }) => {
    const p = globalThis.editProbe, m = p.m, entered = p.defer(), gate = p.defer(), opens = [], computations = [], idRequests = [], results = [];
    const uri = m.URI.parse('inmemory:/pane-selection.ts'), operation = new m.OriginalBulkFileOperation(uri, undefined);
    const range = new m.EditorRange(1, 1, 1, 4); operation.textEdits.push({ textEdit: new m.ResourceTextEdit(uri, { range, text: 'new' }) });
    const oldInput = { fileOperations: [operation] }, oldProvider = { identity: 'old' };
    const pane = new m.BulkEditPane({ _currentInput: oldInput, _currentProvider: oldProvider,
      _computeResourceDiffEditorInputs: { async get(operations) {
        const index = computations.length; computations.push(operations === oldInput.fileOperations);
        if (index === 0 && stage === 'resources') { entered.resolve(); await gate.promise; }
        const resource = m.URI.parse('inmemory:/pane-result-' + index), resources = [{ original: { resource: uri }, modified: { resource } }];
        const result = { resources, async getResourceDiffEditorInputIdOfOperation(value) {
          idRequests.push({ index, sameOperation: value === operation });
          if (index === 0 && stage === 'resourceId') { entered.resolve(); await gate.promise; }
          return { original: uri, modified: resource };
        } }; results.push(result); return result;
      } }, _editorService: { openEditor(input, group) { opens.push({ input, group }); } } });
    const event = () => ({ element: new m.FileElement(pane._currentInput, operation), editorOptions: { preserveFocus: true }, sideBySide: false });
    let settled = false; const pending = pane._openElementInMultiDiffEditor(event()).then(() => { settled = true; });
    await Promise.race([entered.promise, pending.then(() => { throw new Error('未经过原Pane指定异步等待点'); })]);
    // 新 input 刻意复用同一 fileOperations，必须核对象身份而不能只比数组；provider 单独替换同理。
    if (changed === 'input') pane._currentInput = { fileOperations: oldInput.fileOperations }; else pane._currentProvider = { identity: 'fresh' };
    const selectedInput = pane._currentInput, selectedProvider = pane._currentProvider, before = { settled, opens: opens.length };
    gate.resolve(); await pending;
    const stale = { opens: opens.length, idRequests: [...idRequests], sameInput: pane._currentInput === selectedInput, sameProvider: pane._currentProvider === selectedProvider };
    await pane._openElementInMultiDiffEditor(event());
    return { before, stale, computations, idRequests, opens: opens.length, resourcesIdentity: opens[0].input.resources === results[1].resources,
      modified: opens[0].input.options.viewState.revealData.resource.modified.toString(), selectedRange: opens[0].input.options.viewState.revealData.range,
      scheme: opens[0].input.multiDiffSource.scheme, preserveFocus: opens[0].input.options.preserveFocus, failures: p.failures };
  }, { stage, changed });
  expect(result.before).toEqual({ settled: false, opens: 0 }); expect(result.stale).toEqual({ opens: 0, idRequests: stage === 'resources' ? [] : [{ index: 0, sameOperation: true }], sameInput: true, sameProvider: true });
  expect(result.computations).toEqual([true, true]); expect(result.idRequests).toEqual([...(stage === 'resourceId' ? [{ index: 0, sameOperation: true }] : []), { index: 1, sameOperation: true }]);
  expect(result.opens).toBe(1); expect(result.resourcesIdentity).toBe(true); expect(result.modified).toBe('inmemory:/pane-result-1');
  expect(result.selectedRange).toEqual({ startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }); expect(result.scheme).toBe('vscode-bulkeditpreview-multieditor');
  expect(result.preserveFocus).toBe(true); expect(result.failures).toEqual([]); checks.push(`AST原Pane在await ${stage}后${changed}身份变更拒绝旧点击，复用同数组的新点击原路成功且资源身份不变`);
});
