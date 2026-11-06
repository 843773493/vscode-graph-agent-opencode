import { join } from 'node:path';

export const extensionFileEventsSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadFileSystemEventService.ts',
  'src/vs/workbench/api/browser/mainThreadSaveParticipant.ts',
  'src/vs/workbench/api/browser/mainThreadNotebookSaveParticipant.ts',
  'src/vs/workbench/api/browser/mainThreadBulkEdits.ts',
  'src/vs/workbench/contrib/bulkEdit/browser/bulkEditService.ts',
  'src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEdit.contribution.ts',
  'src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPane.ts',
  'src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPreview.ts',
];

export function transformExtensionFileEvents(root, path, source, replaceOnce) {
  const paths = extensionFileEventsSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[7]) {
    let contents = `import { CancellationError, onUnexpectedError } from '../../../../../base/common/errors.js';\n` + source;
    contents = replaceOnce(contents, 'for (const operation of this._operations.fileOperations) {\n\t\t\tawait', 'for (const operation of this._operations.fileOperations) {\n      if (this._disposables.isDisposed) return;\n\t\t\tawait');
    contents = replaceOnce(contents, '\n\t\tthis._disposables.add(Event.debounce(', '\n    if (this._disposables.isDisposed) return;\n\t\tthis._disposables.add(Event.debounce(');
    contents = replaceOnce(contents, 'this._applyTextEditsToPreviewModel(uri);', 'this._applyTextEditsToPreviewModel(uri).catch(onUnexpectedError);');
    contents = replaceOnce(contents, 'const model = await this._getOrCreatePreviewModel(uri);', 'const model = await this._getOrCreatePreviewModel(uri);\n    if (!model || this._disposables.isDisposed) return;');
    contents = replaceOnce(contents, 'const newEdits = await this._operations.getFileEdits(uri);', 'const newEdits = await this._operations.getFileEdits(uri);\n    if (this._disposables.isDisposed) return;');
    contents = replaceOnce(contents, 'private async _getOrCreatePreviewModel(uri: URI) {', 'private async _getOrCreatePreviewModel(uri: URI) {\n    if (this._disposables.isDisposed) return;');
    contents = replaceOnce(contents, 'const ref = await this._textModelResolverService.createModelReference(uri);', 'const ref = await this._textModelResolverService.createModelReference(uri);\n        if (this._disposables.isDisposed) { ref.dispose(); return; }');
    contents = replaceOnce(contents, '} catch {\n\t\t\t\t// create NEW model', '} catch {\n        if (this._disposables.isDisposed) return;\n\t\t\t\t// create NEW model');
    // 微任务只延后原引用获取，不允许已释放的预览重新注册模型；迟到引用由同一 owner 释放。
    contents = replaceOnce(contents, `queueMicrotask(async () => {
\t\t\t\tthis._disposables.add(await this._textModelResolverService.createModelReference(model!.uri));
\t\t\t});`, `queueMicrotask(() => {
        if (this._disposables.isDisposed) { model!.dispose(); return; }
        this._textModelResolverService.createModelReference(model!.uri).then(ref => {
          if (this._disposables.isDisposed) ref.dispose();
          else this._disposables.add(ref);
        }, error => {
          model!.dispose();
          if (!this._disposables.isDisposed) onUnexpectedError(error);
        });
      });`);
    contents = replaceOnce(contents, 'async provideTextContent(previewUri: URI) {', 'async provideTextContent(previewUri: URI) {\n    if (this._disposables.isDisposed) throw new CancellationError();');
    return replaceOnce(contents, 'await this._ready;\n\t\treturn', 'await this._ready;\n    if (this._disposables.isDisposed) throw new CancellationError();\n\t\treturn');
  }
  if (path === paths[5]) {
    let contents = replaceOnce(source, 'import { CancellationTokenSource }', 'import { CancellationToken, CancellationTokenSource }');
    contents = replaceOnce(contents, 'bulkEditService.setPreviewHandler(edits => this._previewEdit(edits));', 'bulkEditService.setPreviewHandler((edits, options) => this._previewEdit(edits, options?.token ?? CancellationToken.None));');
    contents = replaceOnce(contents, 'private async _previewEdit(edits: ResourceEdit[]): Promise<ResourceEdit[]> {\n\t\tthis._ctxEnabled.set(true);', 'private async _previewEdit(edits: ResourceEdit[], token: CancellationToken): Promise<ResourceEdit[]> {\n    if (token.isCancellationRequested) return [];\n    this._ctxEnabled.set(true);');
    contents = replaceOnce(contents, 'const view = await getBulkEditPane(this._viewsService);', `let view: BulkEditPane | undefined;
    try { view = await getBulkEditPane(this._viewsService); }
    catch (error) { if (!this._activeSession) this._ctxEnabled.set(false); throw error; }
    if (token.isCancellationRequested) {
      if (!this._activeSession) this._ctxEnabled.set(false);
      return [];
    }`);
    contents = replaceOnce(contents, '\n\t\t\tthis._ctxEnabled.set(false);\n\t\t\treturn edits;', '\n\t\t\tif (!this._activeSession) this._ctxEnabled.set(false);\n\t\t\treturn edits;');
    contents = replaceOnce(contents, 'if (!confirmed) {', 'if (!confirmed || token.isCancellationRequested) {\n        if (!this._activeSession) this._ctxEnabled.set(false);');
    contents = replaceOnce(contents, `let session: PreviewSession;
\t\tif (this._activeSession) {
\t\t\tawait this._activeSession.uxState.restore(false, true);
\t\t\tthis._activeSession.cts.dispose(true);
\t\t\tsession = new PreviewSession(uxState);
\t\t} else {
\t\t\tsession = new PreviewSession(uxState);
\t\t}`, `const previous = this._activeSession;
    if (previous) {
      await previous.uxState.restore(false, true);
      if (token.isCancellationRequested) {
        if (!this._activeSession) this._ctxEnabled.set(false);
        return [];
      }
      previous.cts.dispose(true);
    }
    const session = new PreviewSession(uxState, new CancellationTokenSource(token));`);
    contents = replaceOnce(contents, 'this._activeSession.cts.dispose();\n\t\t\t\tthis._ctxEnabled.set(false);\n\t\t\t\tthis._activeSession = undefined;', 'if (this._activeSession === session) {\n          this._ctxEnabled.set(false);\n          this._activeSession = undefined;\n        }');
    return replaceOnce(contents, '\n\t\t\t// restore UX state', '\n      session.cts.dispose();\n\t\t\t// restore UX state');
  }
  if (path === paths[6]) {
    let contents = replaceOnce(source, 'async setInput(edit: ResourceEdit[], token: CancellationToken): Promise<ResourceEdit[] | undefined> {', 'async setInput(edit: ResourceEdit[], token: CancellationToken): Promise<ResourceEdit[] | undefined> {\n    if (token.isCancellationRequested) return undefined;');
    contents = replaceOnce(contents, 'const input = await this._instaService.invokeFunction(BulkFileOperations.create, edit);', 'const input = await this._instaService.invokeFunction(BulkFileOperations.create, edit);\n    if (token.isCancellationRequested) { input.dispose(); return undefined; }');
    contents = replaceOnce(contents, '\n\t\t\ttoken.onCancellationRequested(() => resolve(undefined));\n\n\t\t\tthis._currentResolve = resolve;', '\n\t\t\tthis._currentResolve = resolve;\n      this._sessionDisposables.add(token.onCancellationRequested(() => this._done(false)));');
    contents = replaceOnce(contents, 'this._currentResolve?.(accept ? this._currentInput?.getWorkspaceEdit() : undefined);', 'const resolve = this._currentResolve;\n    this._currentResolve = undefined;\n    resolve?.(accept ? this._currentInput?.getWorkspaceEdit() : undefined);');
    contents = replaceOnce(contents, 'const fileOperations = this._currentInput?.fileOperations;', 'const input = this._currentInput;\n    const provider = this._currentProvider;\n    const fileOperations = input?.fileOperations;');
    contents = replaceOnce(contents, 'const result = await this._computeResourceDiffEditorInputs.get(fileOperations);', 'const result = await this._computeResourceDiffEditorInputs.get(fileOperations);\n    if (this._currentInput !== input || this._currentProvider !== provider) return;');
    return replaceOnce(contents, 'const resourceId = await result.getResourceDiffEditorInputIdOfOperation(fileElement.edit);', 'const resourceId = await result.getResourceDiffEditorInputIdOfOperation(fileElement.edit);\n    if (this._currentInput !== input || this._currentProvider !== provider) return;');
  }
  if (path === paths[4]) {
    let contents = `import { IModelService } from '../../../../editor/common/services/model.js';
import { ITextModelService } from '../../../../editor/common/services/resolverService.js';
import { CellUri } from '../../notebook/common/notebookCommon.js';
import { INotebookEditorModelResolverService } from '../../notebook/common/notebookEditorModelResolverService.js';\n` + source;
    contents = replaceOnce(contents, 'import { CancellationToken }', 'import { CancellationToken, CancellationTokenSource }');
    contents = replaceOnce(contents, 'import { IDisposable, toDisposable }', 'import { DisposableStore, IDisposable, toDisposable }');
    // 结果上的注册生命周期在原应用入口合并，预览和所有直接消费者复用同一取消链。
    contents = replaceOnce(contents, '\n\t\tlet edits = liftEdits(', `
    const lifecycleToken = Array.isArray(editsIn) ? undefined : editsIn.lifecycleToken;
    const request = lifecycleToken ? new CancellationTokenSource(options?.token) : undefined;
    const lifecycleCancellation = lifecycleToken?.onCancellationRequested(() => request!.cancel());
    try {
      if (lifecycleToken?.isCancellationRequested || options?.token?.isCancellationRequested) request?.cancel();
      if (request) options = { ...options, token: request.token };
\t\tlet edits = liftEdits(`);
    contents = replaceOnce(contents, 'if (edits.length === 0) {', 'if (edits.length === 0 || options?.token?.isCancellationRequested) {');
    contents = replaceOnce(contents, '\n\t}\n\n\tprivate async _saveAll', '\n    } finally { lifecycleCancellation?.dispose(); request?.dispose(); }\n\t}\n\n\tprivate async _saveAll');
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
