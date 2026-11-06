import { join } from 'node:path';

export const extensionFileEventsSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadFileSystemEventService.ts',
  'src/vs/workbench/api/browser/mainThreadSaveParticipant.ts',
  'src/vs/workbench/api/browser/mainThreadNotebookSaveParticipant.ts',
  'src/vs/workbench/api/browser/mainThreadBulkEdits.ts',
  'src/vs/workbench/contrib/bulkEdit/browser/bulkEditService.ts',
];

export function transformExtensionFileEvents(root, path, source, replaceOnce) {
  const paths = extensionFileEventsSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[4]) {
    let contents = `import { IModelService } from '../../../../editor/common/services/model.js';
import { ITextModelService } from '../../../../editor/common/services/resolverService.js';
import { CellUri } from '../../notebook/common/notebookCommon.js';
import { INotebookEditorModelResolverService } from '../../notebook/common/notebookEditorModelResolverService.js';\n` + source;
    contents = replaceOnce(contents, 'import { IDisposable, toDisposable }', 'import { DisposableStore, IDisposable, toDisposable }');
    contents = replaceOnce(contents, 'if (this._edits.length === 0) {\n\t\t\treturn [];', 'if (this._edits.length === 0 || this._token.isCancellationRequested) {\n\t\t\treturn [];');
    // 最终预览集合先核现有模型版本；原各组临执行检查继续处理随后发生的变化。
    // TODO: Notebook 等宿主尚未提供的版本仍需沿原文档 owner 接线，此处不合成版本。
    contents = replaceOnce(contents, '\n\t\tconst ranges: number[] = [1];', `
    const references = new DisposableStore();
    try {
      const textVersions = new ResourceMap<() => number | undefined>();
      const notebookVersions = new ResourceMap<() => number>();
      const versions: { current: () => number | undefined; expected: number; message: string }[] = [];
      for (const edit of this._edits) {
        if (this._token.isCancellationRequested) return [];
        if (edit instanceof ResourceTextEdit && typeof edit.versionId === 'number') {
          let current = textVersions.get(edit.resource);
          if (!current) {
            const model = this._instaService.invokeFunction(accessor => accessor.get(IModelService).getModel(edit.resource));
            if (model && model.getVersionId() !== edit.versionId) throw new Error(edit.resource.toString() + ' has changed in the meantime');
            // 引用保持到整批完成，防止前序文件操作等待时关闭模型并重置其版本计数。
            const ref = references.add(await this._instaService.invokeFunction(accessor => accessor.get(ITextModelService).createModelReference(edit.resource)));
            if (model && ref.object.textEditorModel !== model) throw new Error(edit.resource.toString() + ' has changed in the meantime');
            current = () => ref.object.textEditorModel.isDisposed() ? undefined : ref.object.textEditorModel.getVersionId();
            textVersions.set(edit.resource, current);
          }
          versions.push({ current, expected: edit.versionId, message: edit.resource.toString() + ' has changed in the meantime' });
        } else if (edit instanceof ResourceNotebookCellEdit && typeof edit.notebookVersionId === 'number') {
          const resource = edit.resource.scheme === CellUri.scheme ? CellUri.parse(edit.resource)?.notebook : edit.resource;
          if (!resource) throw new Error('Invalid notebook URI: ' + edit.resource.toString());
          let current = notebookVersions.get(resource);
          if (!current) {
            const ref = references.add(await this._instaService.invokeFunction(accessor => accessor.get(INotebookEditorModelResolverService).resolve(resource)));
            current = () => ref.object.notebook.versionId;
            notebookVersions.set(resource, current);
          }
          versions.push({ current, expected: edit.notebookVersionId, message: "Notebook '" + resource.toString() + "' has changed in the meantime" });
        }
      }
      if (this._token.isCancellationRequested) return [];
      // 所有异步解析结算后同一轮核对，后一次解析不能掩盖先前模型的变化。
      for (const version of versions) if (version.current() !== version.expected) throw new Error(version.message);
\t\tconst ranges: number[] = [1];`);
    contents = replaceOnce(contents, 'return resources.flat();', 'return resources.flat();\n    } finally { references.dispose(); }');
    return replaceOnce(contents, 'isApplied: edits.length > 0 };', 'isApplied: edits.length > 0 && !(options?.token?.isCancellationRequested && resources.length === 0) };');
  }
  if (path === paths[3]) {
    let contents = `import { assertHostWorkspaceEdit } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};
import { CancellationTokenSource } from '../../../base/common/cancellation.js';\n` + source;
    contents = replaceOnce(contents, 'export class MainThreadBulkEdits implements MainThreadBulkEditsShape {', `export class MainThreadBulkEdits implements MainThreadBulkEditsShape {
  private readonly _lifetime = new CancellationTokenSource();
  private _disposed = false;`);
    contents = replaceOnce(contents, '_extHostContext: IExtHostContext,', 'private readonly _extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, 'dispose(): void { }', 'dispose(): void { this._disposed = true; this._lifetime.cancel(); this._lifetime.dispose(); }');
    contents = replaceOnce(contents, 'const edits = reviveWorkspaceEditDto(dto.value, this._uriIdentService);', `return Promise.resolve().then(() => {
      if (this._disposed) throw new Error('扩展编辑所属宿主代际已经关闭');
      assertHostWorkspaceEdit(this._extHostContext.extensionHost, dto.value);
      const edits = reviveWorkspaceEditDto(dto.value, this._uriIdentService);
      // 无版本字段的文件／opaque 操作继续沿原 FileService 合同，不合成另一套版本。`);
    return replaceOnce(contents, 'respectAutoSaveConfig: isRefactoring }).then((res)', 'respectAutoSaveConfig: isRefactoring, token: this._lifetime.token });\n    }).then((res)');
  }
  if (path === paths[1] || path === paths[2]) {
    let contents = `import { hostOwnsFileEventResource } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};\n` + source;
    const resource = path === paths[1] ? 'editorModel.resource' : 'workingCopy.resource';
    const notebook = path === paths[2];
    const proxy = notebook ? 'ExtHostNotebookDocumentSaveParticipant' : 'ExtHostDocumentSaveParticipant';
    contents = replaceOnce(contents, notebook ? 'private readonly _proxy: ExtHostNotebookDocumentSaveParticipantShape;' : 'private readonly _proxy: ExtHostDocumentSaveParticipantShape;', `${notebook ? 'private readonly _proxy: ExtHostNotebookDocumentSaveParticipantShape;' : 'private readonly _proxy: ExtHostDocumentSaveParticipantShape;'}\n\tprivate readonly _host: IExtHostContext['extensionHost'];`);
    contents = replaceOnce(contents, `this._proxy = extHostContext.getProxy(ExtHostContext.${proxy});`, `this._proxy = extHostContext.getProxy(ExtHostContext.${proxy});\n\t\tthis._host = extHostContext.extensionHost;`);
    const anchor = path === paths[1]
      ? 'if (!editorModel.textEditorModel || !shouldSynchronizeModel(editorModel.textEditorModel)) {'
      : 'if (!workingCopy.model || !(workingCopy.model instanceof NotebookFileWorkingCopyModel)) {';
    return replaceOnce(contents, anchor, `if (!hostOwnsFileEventResource(this._host, ${resource})) return undefined;\n\t\t${anchor}`);
  }
  if (path !== paths[0]) return source;
  let contents = `import { assertHostWorkspaceEdit, hostOwnsFileEventResource } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};\n` + source;
  contents = replaceOnce(contents, 'private readonly _proxy: ExtHostFileSystemEventServiceShape;', `private readonly _proxy: ExtHostFileSystemEventServiceShape;
  private readonly _host: IExtHostContext['extensionHost'];
  private _disposed = false;

  private accepts(uri: URI): boolean { return hostOwnsFileEventResource(this._host, uri); }
  private acceptsWatch(uri: URI): boolean { return hostOwnsFileEventResource(this._host, uri, true); }
  private projectEvent(event: { created: UriComponents[]; changed: UriComponents[]; deleted: UriComponents[]; session?: number }) {
    if (this._disposed) throw new Error('文件事件所属宿主代际已经关闭');
    const project = (values: UriComponents[]) => values.map(value => URI.revive(value)).filter((uri): uri is URI => !!uri && this.accepts(uri)).map(uri => uri.toJSON());
    return { ...event, created: project(event.created), changed: project(event.changed), deleted: project(event.deleted) };
  }

`);
  contents = replaceOnce(contents, 'this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostFileSystemEventService);', `this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostFileSystemEventService);
    this._host = extHostContext.extensionHost;`);
  contents = replaceOnce(contents, `this._proxy.$onFileEvent({
				created: event.rawAdded,
				changed: event.rawUpdated,
				deleted: event.rawDeleted
			});`, `if (!this._disposed) this._proxy.$onFileEvent(this.projectEvent({ created: event.rawAdded,
				changed: event.rawUpdated, deleted: event.rawDeleted }));`);

  contents = replaceOnce(contents, 'const onWillEvent = that._proxy.$onWillRunFileOperation(operation, files, timeout, cts.token);', `const ownedFiles = files.filter(file => (!file.source || that.accepts(file.source)) && that.accepts(file.target));
					if (!ownedFiles.length) return Promise.resolve(undefined);
					const onWillEvent = that._proxy.$onWillRunFileOperation(operation, ownedFiles, timeout, cts.token);`);
  contents = replaceOnce(contents, 'await bulkEditService.apply(\n\t\t\t\t\treviveWorkspaceEditDto(data.edit, uriIdentService),', `if (that._disposed) throw new Error('文件操作参与者所属宿主代际已经关闭');
          assertHostWorkspaceEdit(that._host, data.edit);
          await bulkEditService.apply(
\t\t\t\t\treviveWorkspaceEditDto(data.edit, uriIdentService),`);
  contents = replaceOnce(contents, 'workingCopyFileService.onDidRunWorkingCopyFileOperation(e => this._proxy.$onDidRunFileOperation(e.operation, e.files))', 'workingCopyFileService.onDidRunWorkingCopyFileOperation(e => { if (this._disposed) return; const files = e.files.filter(file => (!file.source || this.accepts(file.source)) && this.accepts(file.target)); if (files.length) this._proxy.$onDidRunFileOperation(e.operation, files); })');
  contents = replaceOnce(contents, 'const uri = URI.revive(resource);', `if (this._disposed) throw new Error('文件监听所属宿主代际已经关闭');
		const uri = URI.revive(resource);
		if (!this.acceptsWatch(uri)) throw new Error('文件监听超出发起宿主工作区视图：' + uri.toString());
`);
  contents = replaceOnce(contents, 'const canHandleWatcher = await this._fileService.canHandleResource(uri);', 'const canHandleWatcher = await this._fileService.canHandleResource(uri);\n\t\tif (this._disposed) throw new Error(\'文件监听所属宿主代际已经关闭\');');
  contents = replaceOnce(contents, 'this._watches.set(session, watcherDisposables);', 'if (this._disposed) { watcherDisposables.dispose(); throw new Error(\'文件监听所属宿主代际已经关闭\'); }\n\t\t\tthis._watches.set(session, watcherDisposables);');
  contents = replaceOnce(contents, 'this._watches.set(session, subscription);', 'if (this._disposed) { subscription.dispose(); throw new Error(\'文件监听所属宿主代际已经关闭\'); }\n\t\t\tthis._watches.set(session, subscription);');
  contents = replaceOnce(contents, `this._proxy.$onFileEvent({
					session,
					created: event.rawAdded,
					changed: event.rawUpdated,
					deleted: event.rawDeleted
				});`, `if (!this._disposed) this._proxy.$onFileEvent(this.projectEvent({ session,
					created: event.rawAdded, changed: event.rawUpdated, deleted: event.rawDeleted }));`);
  contents = replaceOnce(contents, '\tdispose(): void {\n\t\tthis._listener.dispose();', '\tdispose(): void {\n    this._disposed = true;\n\t\tthis._listener.dispose();');
  return contents;
}
