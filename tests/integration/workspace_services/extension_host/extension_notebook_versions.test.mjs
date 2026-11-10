import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_notebook_versions.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const sources = ['scripts/extensions/extensionLanguageFeaturesTransform.mjs',
  ...['extHost.api.impl', 'extHostBulkEdits', 'extHostLanguageFeatures', 'extHostTypeConverters', 'extHostNotebook',
    'extHostNotebookDocuments', 'extHostNotebookDocument', 'extHostFileSystemEventService', 'extHostNotebookDocumentSaveParticipant']
    .map(name => 'upstream/src/vs/workbench/api/common/' + name + '.ts')];
const fingerprints = Object.fromEntries(await Promise.all(sources.map(async path => [path,
  createHash('sha256').update(await readFile(join(root, path))).digest('hex')])));
const bundle = join(run.artifactsDir, 'notebook-versions.fixture.mjs');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const exports = [
  'ExtHostDocumentsAndEditors:workbench/api/common/extHostDocumentsAndEditors',
  'ExtHostDocuments:workbench/api/common/extHostDocuments',
  'ExtHostNotebookController:workbench/api/common/extHostNotebook',
  'ExtHostNotebookDocuments:workbench/api/common/extHostNotebookDocuments',
  'ExtHostNotebookDocumentSaveParticipant:workbench/api/common/extHostNotebookDocumentSaveParticipant',
  'ExtHostFileSystemEventService:workbench/api/common/extHostFileSystemEventService',
  'ExtHostBulkEdits:workbench/api/common/extHostBulkEdits',
  'ExtHostLanguageFeatures:workbench/api/common/extHostLanguageFeatures',
  'CommandsConverter:workbench/api/common/extHostCommands',
  'WorkspaceEdit, NotebookEdit, Range, CodeAction, CodeActionKind:workbench/api/common/extHostTypes',
  'MainContext:workbench/api/common/extHost.protocol',
  'SerializableObjectWithBuffers:workbench/services/extensions/common/proxyIdentifier',
  'CancellationTokenSource, CancellationToken:base/common/cancellation',
  'NullLogService:platform/log/common/log',
  'FileOperation:platform/files/common/files',
  'URI:base/common/uri',
].map(entry => {
  const [names, path] = entry.split(':');
  return 'export { ' + names + ' } from \'' + upstream + path + '.js\';';
}).join('\n');
await build({ stdin: { loader: 'ts', resolveDir: resolve('.'), contents: exports }, outfile: bundle, bundle: true, format: 'esm',
  platform: 'node', target: 'es2024', plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(bundle);
const extension = { identifier: { value: 'probe.notebook-versions' }, name: 'notebook-versions', enabledApiProposals: [] };
const cleanups = [], checks = [], observations = [];

afterAll(async () => {
  for (const cleanup of cleanups.reverse()) cleanup();
  await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, checks, observations,
    boundary: '通过原 ExtHost NotebookController/NotebookDocuments、CodeAction/Rename 注册、文件与 Notebook 保存参与者、WorkspaceEdit.from 和直接 apply 验证窗口文档版本快照与取消；不覆盖 Gateway、旧 Node Extension Host、磁盘写入或 Notebook UI 应用。' }, null, 2));
  await run.close();
});

function createRuntime() {
  const calls = [], registered = {}, log = new m.NullLogService();
  const main = {
    $tryApplyWorkspaceEdit(value) { calls.push(value.value); return Promise.resolve(true); },
    $registerCodeActionSupport(handle) { registered.action = handle; },
    $registerRenameSupport(handle) { registered.rename = handle; },
    $unregister() {},
  };
  const context = { getProxy: id => id === m.MainContext.MainThreadLanguageFeatures || id === m.MainContext.MainThreadBulkEdits ? main : {} };
  const documentOwner = new m.ExtHostDocumentsAndEditors(context, log), documents = new m.ExtHostDocuments(context, documentOwner);
  const converter = new m.CommandsConverter({ registerCommand: () => ({ dispose() {} }) }, () => undefined, log);
  const commands = { converter, registerArgumentProcessor() {}, registerApiCommand() {} };
  const notebooks = new m.ExtHostNotebookController(context, commands, documentOwner, documents, undefined, undefined, log);
  const notebookDocuments = new m.ExtHostNotebookDocuments(notebooks);
  const features = new m.ExtHostLanguageFeatures(context, undefined, documents, notebooks, commands, { getDiagnostics: () => [] }, log,
    { report() { throw new Error('本场景不应调用弃用 API'); } }, { onExtensionError(_extension, error) { throw error; } });
  const bulk = new m.ExtHostBulkEdits(context, documentOwner, notebooks);
  const files = new m.ExtHostFileSystemEventService(context, log, documentOwner, notebooks);
  const saves = new m.ExtHostNotebookDocumentSaveParticipant(log, notebooks, main, documentOwner);
  cleanups.push(() => {
    notebooks.$acceptDocumentAndEditorsDelta(new m.SerializableObjectWithBuffers({ removedDocuments: notebooks.notebookDocuments.map(data => data.uri) }));
    documentOwner.acceptDocumentsAndEditorsDelta({ removedDocuments: documents.getAllDocumentData().map(data => data.document.uri) });
    documents.dispose(); features.dispose(); saves.dispose();
  });
  return { calls, registered, features, bulk, files, saves, notebooks,
    addNotebook(uri, version) {
      notebooks.$acceptDocumentAndEditorsDelta(new m.SerializableObjectWithBuffers({ addedDocuments: [{ uri, versionId: version,
        viewType: 'probe', cells: [], metadata: {}, isDirty: false }] }));
    },
    changeNotebook(uri, version) {
      notebookDocuments.$acceptModelChanged(uri, new m.SerializableObjectWithBuffers({ versionId: version, rawEvents: [] }), true);
    },
    removeNotebook(uri) { notebooks.$acceptDocumentAndEditorsDelta(new m.SerializableObjectWithBuffers({ removedDocuments: [uri] })); },
    addText(uri, version) {
      documentOwner.acceptDocumentsAndEditorsDelta({ addedDocuments: [{ uri, versionId: version, lines: ['old'], EOL: '\n',
        languageId: 'typescript', isDirty: false, encoding: 'utf8' }] });
    },
    changeText(uri, version) {
      documents.$acceptModelChanged(uri, { versionId: version, changes: [], eol: '\n', isUndoing: false, isRedoing: false, isFlush: false, isEolChange: false }, true);
    },
  };
}

const notebook = m.URI.file('/workspace/shared.ipynb'), anotherNotebook = m.URI.file('/workspace/other.ipynb');
const text = m.URI.file('/workspace/source.ts');
function editFor(...resources) {
  const edit = new m.WorkspaceEdit();
  for (const uri of resources) edit.set(uri, [m.NotebookEdit.updateNotebookMetadata({ changed: true })]);
  edit.replace(text, new m.Range(0, 0, 0, 1), 'new');
  return edit;
}
const versions = dto => dto.edits.map(edit => edit.notebookVersionId ?? edit.versionId ?? null);

test('直接 apply 从当前文档快照读取 Notebook 与文本版本', async () => {
  const runtime = createRuntime(), unknown = m.URI.file('/workspace/unknown.ipynb');
  runtime.addNotebook(notebook, 3); runtime.addNotebook(anotherNotebook, 19); runtime.addText(text, 7);
  expect(await runtime.bulk.applyWorkspaceEdit(editFor(notebook, anotherNotebook, unknown), extension)).toBe(true);
  runtime.changeNotebook(notebook, 4);
  await runtime.bulk.applyWorkspaceEdit(editFor(notebook, anotherNotebook, unknown), extension);
  runtime.removeNotebook(notebook);
  await runtime.bulk.applyWorkspaceEdit(editFor(notebook, anotherNotebook, unknown), extension);
  expect(runtime.calls.map(versions)).toEqual([
    [3, 19, null, 7], [4, 19, null, 7], [null, 19, null, 7],
  ]);
  observations.push({ directApplyVersions: runtime.calls.map(versions) });
  checks.push('直接 WorkspaceEdit.from 捕获窗口内当前 Notebook/文本版本；未知和已关闭 Notebook 不生成版本');
});

test('CodeAction provide 和 resolve 各自冻结异步回调开始前的文档版本', async () => {
  const runtime = createRuntime(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  runtime.addNotebook(notebook, 4); runtime.addText(text, 7);
  const action = new m.CodeAction('Notebook 编辑', m.CodeActionKind.QuickFix);
  const provider = {
    async provideCodeActions() { entered.resolve(); await gate.promise; action.edit = editFor(notebook); return [action]; },
    async resolveCodeAction(item) { entered.resolve(); await gate.promise; item.edit = editFor(notebook); return item; },
  };
  const registration = runtime.features.registerCodeActionProvider(extension, ['typescript'], provider, {});
  const pending = runtime.features.$provideCodeActions(runtime.registered.action, text,
    { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }, { trigger: 1 }, m.CancellationToken.None);
  await entered.promise; runtime.changeNotebook(notebook, 5); runtime.changeText(text, 8); gate.resolve();
  const list = await pending;
  expect(versions(list.actions[0].edit)).toEqual([4, 7]);

  delete action.edit;
  const resolveEntered = Promise.withResolvers(), resolveGate = Promise.withResolvers();
  provider.resolveCodeAction = async item => { resolveEntered.resolve(); await resolveGate.promise; item.edit = editFor(notebook); return item; };
  const resolving = runtime.features.$resolveCodeAction(runtime.registered.action, [list.cacheId, 0], m.CancellationToken.None);
  await resolveEntered.promise; runtime.changeNotebook(notebook, 6); runtime.changeText(text, 9); resolveGate.resolve();
  expect(versions((await resolving).edit)).toEqual([5, 8]);
  runtime.features.$releaseCodeActions(runtime.registered.action, list.cacheId); registration.dispose();
  checks.push('CodeActionAdapter provide/resolve 在各自回调前冻结版本，不把回调期间的新版本贴给旧编辑');
});

test('Rename 编辑返回后仍使用调用前 Notebook 版本', async () => {
  const runtime = createRuntime(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  runtime.addNotebook(notebook, 12); runtime.addText(text, 7);
  const registration = runtime.features.registerRenameProvider(extension, ['typescript'], {
    async provideRenameEdits() { entered.resolve(); await gate.promise; return editFor(notebook); },
  });
  const pending = runtime.features.$provideRenameEdits(runtime.registered.rename, text, { lineNumber: 1, column: 1 }, 'renamed', m.CancellationToken.None);
  await entered.promise; runtime.changeNotebook(notebook, 13); gate.resolve();
  expect(versions(await pending)).toEqual([12, 7]); registration.dispose();
  checks.push('RenameAdapter 使用调用前版本，不认证异步回调返回时的新版');
});

test('文件参与者等待期间新建的文档不进入本次 WorkspaceEdit 版本快照', async () => {
  const runtime = createRuntime(), entered = Promise.withResolvers(), gate = Promise.withResolvers(), late = m.URI.file('/workspace/late.ipynb');
  runtime.addNotebook(notebook, 20); runtime.addText(text, 7);
  const subscription = runtime.files.getOnWillCreateFileEvent(extension)(event => event.waitUntil((async () => {
    entered.resolve(); await gate.promise; return editFor(notebook, late);
  })()));
  const pending = runtime.files.$onWillRunFileOperation(m.FileOperation.CREATE, [{ target: m.URI.file('/workspace/new.ts') }], 1000, m.CancellationToken.None);
  await entered.promise; runtime.changeNotebook(notebook, 21); runtime.changeText(text, 8); runtime.addNotebook(late, 30); gate.resolve();
  expect(versions((await pending).edit)).toEqual([20, null, 7]);
  subscription.dispose();
  checks.push('文件参与者在事件开始前冻结完整文档版本，异步 waitUntil 不将迟到 Notebook 纳入快照');
});

test('Notebook 保存参与者传递调用前版本，取消不应用 WorkspaceEdit', async () => {
  const runtime = createRuntime(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  runtime.addNotebook(notebook, 40); runtime.addText(text, 7);
  const subscription = runtime.saves.getOnWillSaveNotebookDocumentEvent(extension)(event => event.waitUntil((async () => {
    entered.resolve(); await gate.promise; return editFor(notebook);
  })()));
  const pending = runtime.saves.$participateInSave(notebook, 1, m.CancellationToken.None);
  await entered.promise; runtime.changeNotebook(notebook, 41); runtime.changeText(text, 8); gate.resolve();
  expect(await pending).toBe(true); expect(runtime.calls.map(versions)).toEqual([[40, 7]]);
  subscription.dispose();
  const cancelled = new m.CancellationTokenSource(); cancelled.cancel();
  expect(await runtime.saves.$participateInSave(notebook, 1, cancelled.token)).toBe(false);
  expect(runtime.calls).toHaveLength(1); cancelled.dispose();
  checks.push('Notebook 保存事件沿原 WorkspaceEdit 转换器使用冻结版本；已取消请求无应用');
});
