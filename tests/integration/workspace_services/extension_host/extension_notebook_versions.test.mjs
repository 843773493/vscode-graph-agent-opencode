import { afterAll, describe, expect, test } from 'bun:test';
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
    .map(name => `upstream/src/vs/workbench/api/common/${name}.ts`)];
const fingerprints = Object.fromEntries(await Promise.all(sources.map(async path => [path,
  createHash('sha256').update(await readFile(join(root, path))).digest('hex')])));
const modules = [];
for (const runtime of ['browser', 'node']) {
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, [], { runtime });
const bundle = join(run.artifactsDir, `notebook-versions-${runtime}.fixture.mjs`);
await build({ stdin: { loader: 'ts', resolveDir: resolve('.'), contents: `
export { ExtHostDocumentsAndEditors } from '${upstream}workbench/api/common/extHostDocumentsAndEditors.js';
export { ExtHostDocuments } from '${upstream}workbench/api/common/extHostDocuments.js';
export { ExtHostNotebookController } from '${upstream}workbench/api/common/extHostNotebook.js';
export { ExtHostNotebookDocuments } from '${upstream}workbench/api/common/extHostNotebookDocuments.js';
export { ExtHostNotebookDocumentSaveParticipant } from '${upstream}workbench/api/common/extHostNotebookDocumentSaveParticipant.js';
export { ExtHostFileSystemEventService } from '${upstream}workbench/api/common/extHostFileSystemEventService.js';
export { ExtHostBulkEdits } from '${upstream}workbench/api/common/extHostBulkEdits.js';
export { ExtHostLanguageFeatures } from '${upstream}workbench/api/common/extHostLanguageFeatures.js';
export { CommandsConverter } from '${upstream}workbench/api/common/extHostCommands.js';
export { WorkspaceEdit, NotebookEdit, Range, CodeAction, CodeActionKind } from '${upstream}workbench/api/common/extHostTypes.js';
export { MainContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { SerializableObjectWithBuffers } from '${upstream}workbench/services/extensions/common/proxyIdentifier.js';
export { CancellationTokenSource, CancellationToken } from '${upstream}base/common/cancellation.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { FileOperation } from '${upstream}platform/files/common/files.js';
export { URI } from '${upstream}base/common/uri.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
modules.push([runtime, await import(bundle)]);
}
const extension = { identifier: { value: 'probe.notebook-versions' }, name: 'notebook-versions', enabledApiProposals: [] };
const cleanups = [], checks = [], observations = [];
afterAll(async () => {
  for (const cleanup of cleanups.reverse()) cleanup();
  await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ fingerprints, checks, observations,
    boundary: '原ExtHost NotebookController/NotebookDocuments、文档delta/change、CodeAction/Rename公开注册、文件及Notebook保存事件、WorkspaceEdit.from和直接apply；MainThread回执、扩展回调与命令登记是具名边界。版本由原文档owner生产，不手写进DTO；本组不代替Gateway、目标Notebook投影、真实Node进程、Notebook模型应用或原生平台验收。' }, null, 2));
});

for (const [runtime, m] of modules) describe(runtime, () => {
function host() {
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
    { report() { throw new Error('本场景不应调用弃用API'); } }, { onExtensionError(_extension, error) { throw error; } });
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
const notebook = m.URI.file('/workspace/shared.ipynb'), text = m.URI.file('/workspace/source.ts');
function editFor(...resources) {
  const edit = new m.WorkspaceEdit();
  for (const uri of resources) edit.set(uri, [m.NotebookEdit.updateNotebookMetadata({ changed: true })]);
  edit.replace(text, new m.Range(0, 0, 0, 1), 'new');
  return edit;
}
const versions = dto => dto.edits.map(edit => edit.notebookVersionId ?? edit.versionId ?? null);

test('直接 apply 使用当前宿主的真实 Notebook 版本，关闭或未同步资源不借用其它宿主', async () => {
  const a = host(), b = host(), unknown = m.URI.file('/workspace/unknown.ipynb');
  a.addNotebook(notebook, 3); b.addNotebook(notebook, 19); a.addText(text, 7);
  expect(await a.bulk.applyWorkspaceEdit(editFor(notebook, unknown), extension)).toBe(true);
  expect(await b.bulk.applyWorkspaceEdit(editFor(notebook), extension)).toBe(true);
  a.changeNotebook(notebook, 4); await a.bulk.applyWorkspaceEdit(editFor(notebook), extension);
  a.removeNotebook(notebook); await a.bulk.applyWorkspaceEdit(editFor(notebook), extension);
  expect(a.calls.map(versions)).toEqual([[3, null, 7], [4, 7], [null, 7]]);
  expect(b.calls.map(versions)).toEqual([[19, null]]);
  observations.push({ directA: a.calls.map(versions), directB: b.calls.map(versions) });
  checks.push('直接apply消费原Notebook owner、同URI宿主隔离、真实change及关闭/未知无版本');
});

test('CodeAction provide 与 resolve 在调用前分别冻结 Notebook 和文本版本', async () => {
  const a = host(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  a.addNotebook(notebook, 4); a.addText(text, 7);
  const action = new m.CodeAction('Notebook 编辑', m.CodeActionKind.QuickFix);
  const provider = {
    async provideCodeActions() { entered.resolve(); await gate.promise; action.edit = editFor(notebook); return [action]; },
    async resolveCodeAction(item) { entered.resolve(); await gate.promise; item.edit = editFor(notebook); return item; },
  };
  const registration = a.features.registerCodeActionProvider(extension, ['typescript'], provider, {});
  const pending = a.features.$provideCodeActions(a.registered.action, text, { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 },
    { trigger: 1 }, m.CancellationToken.None);
  await entered.promise; a.changeNotebook(notebook, 5); a.changeText(text, 8); gate.resolve();
  const list = await pending;
  expect(versions(list.actions[0].edit)).toEqual([4, 7]);
  delete action.edit;
  const resolveEntered = Promise.withResolvers(), resolveGate = Promise.withResolvers();
  provider.resolveCodeAction = async item => { resolveEntered.resolve(); await resolveGate.promise; item.edit = editFor(notebook); return item; };
  const resolving = a.features.$resolveCodeAction(a.registered.action, [list.cacheId, 0], m.CancellationToken.None);
  await resolveEntered.promise; a.changeNotebook(notebook, 6); a.changeText(text, 9); resolveGate.resolve();
  expect(versions((await resolving).edit)).toEqual([5, 8]);
  a.features.$releaseCodeActions(a.registered.action, list.cacheId); registration.dispose();
  checks.push('原CodeActionAdapter provide/resolve在回调前各自冻结Notebook/文本版本，不给旧编辑贴新版本');
});

test('Rename 回调等待期间的 Notebook 变更不会覆盖原调用版本', async () => {
  const a = host(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  a.addNotebook(notebook, 12); a.addText(text, 7);
  const registration = a.features.registerRenameProvider(extension, ['typescript'], {
    async provideRenameEdits() { entered.resolve(); await gate.promise; return editFor(notebook); },
  });
  const pending = a.features.$provideRenameEdits(a.registered.rename, text, { lineNumber: 1, column: 1 }, 'renamed', m.CancellationToken.None);
  await entered.promise; a.changeNotebook(notebook, 13); gate.resolve();
  expect(versions(await pending)).toEqual([12, 7]); registration.dispose();
  checks.push('原RenameAdapter与同一转换器保留调用前Notebook版本');
});

test('文件参与者先冻结整次事件版本，异步 waitUntil 不能认证等待期间的新文档', async () => {
  const a = host(), entered = Promise.withResolvers(), gate = Promise.withResolvers(), late = m.URI.file('/workspace/late.ipynb');
  a.addNotebook(notebook, 20); a.addText(text, 7);
  const subscription = a.files.getOnWillCreateFileEvent(extension)(event => event.waitUntil((async () => {
    entered.resolve(); await gate.promise; return editFor(notebook, late);
  })()));
  const pending = a.files.$onWillRunFileOperation(m.FileOperation.CREATE, [{ target: m.URI.file('/workspace/new.ts') }], 1000, m.CancellationToken.None);
  await entered.promise; a.changeNotebook(notebook, 21); a.changeText(text, 8); a.addNotebook(late, 30); gate.resolve();
  expect(versions((await pending).edit)).toEqual([20, null, 7]); subscription.dispose();
  checks.push('原文件参与者在事件前冻结，异步waitUntil后新Notebook不伪造版本');
});

test('Notebook 保存参与者复用同一版本生产链，取消仍零应用', async () => {
  const a = host(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  a.addNotebook(notebook, 40); a.addText(text, 7);
  const subscription = a.saves.getOnWillSaveNotebookDocumentEvent(extension)(event => event.waitUntil((async () => {
    entered.resolve(); await gate.promise; return editFor(notebook);
  })()));
  const pending = a.saves.$participateInSave(notebook, 1, m.CancellationToken.None);
  await entered.promise; a.changeNotebook(notebook, 41); a.changeText(text, 8); gate.resolve();
  expect(await pending).toBe(true); expect(a.calls.map(versions)).toEqual([[40, 7]]);
  subscription.dispose();
  const cancelled = new m.CancellationTokenSource(); cancelled.cancel();
  expect(await a.saves.$participateInSave(notebook, 1, cancelled.token)).toBe(false);
  expect(a.calls).toHaveLength(1); cancelled.dispose();
  checks.push('原Notebook保存事件/WorkspaceEdit.from传入调用前Notebook与文本版本，取消零应用');
});
});
