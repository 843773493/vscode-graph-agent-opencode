import { join } from 'node:path';
export const extensionLanguageFeaturesSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts',
  'src/vs/workbench/api/common/extHostLanguageFeatures.ts',
  'src/vs/workbench/api/common/extHostTypeConverters.ts',
  'src/vs/workbench/api/common/extHostBulkEdits.ts',
  'src/vs/workbench/api/common/extHost.api.impl.ts',
  'src/vs/workbench/api/common/extHostFileSystemEventService.ts',
  'src/vs/workbench/api/common/extHostNotebookDocumentSaveParticipant.ts',
	'src/vs/editor/contrib/codeAction/browser/codeAction.ts',
	'src/vs/editor/contrib/rename/browser/rename.ts',
	'src/vs/editor/contrib/inlineCompletions/browser/model/renameSymbolProcessor.ts',
	'src/vs/editor/common/languages.ts',
	'src/vs/editor/contrib/codeAction/browser/codeActionController.ts',
];

export function transformExtensionLanguageFeatures(root, path, source, replaceOnce) {
  const paths = extensionLanguageFeaturesSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[2]) {
    return replaceOnce(source, 'export namespace WorkspaceEdit {', `export namespace WorkspaceEdit {
  /** 只冻结已同步的数值版本；异步回调期间的新版本不能认证旧编辑。 */
  export function captureVersions(textDocuments: Iterable<{ readonly document: { readonly uri: URI }; readonly version: number }>,
    notebooks: Iterable<{ readonly uri: URI; readonly versionId: number }>): IVersionInformationProvider {
    const textVersions = new ResourceMap<number>();
    const notebookVersions = new ResourceMap<number>();
    for (const data of textDocuments) textVersions.set(data.document.uri, data.version);
    for (const data of notebooks) notebookVersions.set(data.uri, data.versionId);
    return {
      getTextDocumentVersion: uri => textVersions.get(uri),
      getNotebookDocumentVersion: uri => notebookVersions.get(uri),
    };
  }
`);
  }
  if (path === paths[3]) {
    let contents = `import type { ExtHostNotebookController } from './extHostNotebook.js';\n` + source;
    contents = replaceOnce(contents, 'extHostDocumentsAndEditors: ExtHostDocumentsAndEditors,',
      'extHostDocumentsAndEditors: ExtHostDocumentsAndEditors,\n    notebooks: ExtHostNotebookController,');
    return replaceOnce(contents, 'getNotebookDocumentVersion: () => undefined',
      'getNotebookDocumentVersion: uri => notebooks.getNotebookDocument(uri, true)?.versionId');
  }
  if (path === paths[4]) {
    let contents = replaceOnce(source, 'new ExtHostLanguageFeatures(rpcProtocol, uriTransformer, extHostDocuments,',
      'new ExtHostLanguageFeatures(rpcProtocol, uriTransformer, extHostDocuments, extHostNotebook,');
    contents = replaceOnce(contents, 'new ExtHostBulkEdits(rpcProtocol, extHostDocumentsAndEditors)', 'new ExtHostBulkEdits(rpcProtocol, extHostDocumentsAndEditors, extHostNotebook)');
    contents = replaceOnce(contents, 'new ExtHostFileSystemEventService(rpcProtocol, extHostLogService, extHostDocumentsAndEditors)',
      'new ExtHostFileSystemEventService(rpcProtocol, extHostLogService, extHostDocumentsAndEditors, extHostNotebook)');
    return replaceOnce(contents, 'new ExtHostNotebookDocumentSaveParticipant(extHostLogService, extHostNotebook, rpcProtocol.getProxy(MainContext.MainThreadBulkEdits))',
      'new ExtHostNotebookDocumentSaveParticipant(extHostLogService, extHostNotebook, rpcProtocol.getProxy(MainContext.MainThreadBulkEdits), extHostDocumentsAndEditors)');
  }
  if (path === paths[5]) {
    let contents = `import type { ExtHostNotebookController } from './extHostNotebook.js';\n` + source;
    contents = replaceOnce(contents, 'private readonly _extHostDocumentsAndEditors: ExtHostDocumentsAndEditors\n',
      'private readonly _extHostDocumentsAndEditors: ExtHostDocumentsAndEditors,\n    private readonly _notebooks: ExtHostNotebookController\n');
    contents = replaceOnce(contents, 'const extensionNames = new Set<string>();',
      'const versions = typeConverter.WorkspaceEdit.captureVersions(this._extHostDocumentsAndEditors.allDocuments(), this._notebooks.notebookDocuments);\n\t\tconst extensionNames = new Set<string>();');
    return replaceOnce(contents, `typeConverter.WorkspaceEdit.from(edit, {
\t\t\t\tgetTextDocumentVersion: uri => this._extHostDocumentsAndEditors.getDocument(uri)?.version,
\t\t\t\tgetNotebookDocumentVersion: () => undefined,
\t\t\t})`, 'typeConverter.WorkspaceEdit.from(edit, versions)');
  }
  if (path === paths[6]) {
    let contents = `import type { ExtHostDocumentsAndEditors } from './extHostDocumentsAndEditors.js';\n` + source;
    contents = replaceOnce(contents, 'private readonly _mainThreadBulkEdits: MainThreadBulkEditsShape,',
      'private readonly _mainThreadBulkEdits: MainThreadBulkEditsShape,\n    private readonly _textDocuments: ExtHostDocumentsAndEditors,');
    contents = replaceOnce(contents, 'const edits: WorkspaceEdit[] = [];',
      'const versions = WorksapceEditConverter.captureVersions(this._textDocuments.allDocuments(), this._notebooksAndEditors.notebookDocuments);\n\t\tconst edits: WorkspaceEdit[] = [];');
    return replaceOnce(contents, 'WorksapceEditConverter.from(edit)', 'WorksapceEditConverter.from(edit, versions)');
  }
  if (path === paths[1]) {
    let contents = `import type { ExtHostNotebookController } from './extHostNotebook.js';\n` + source;
    for (const name of ['CodeActionAdapter', 'RenameAdapter', 'ExtHostLanguageFeatures']) {
      const start = contents.indexOf('class ' + name + ' '), constructor = contents.indexOf('constructor(', start);
      const parameter = contents.indexOf('private readonly _documents: ExtHostDocuments,', constructor);
      if (start < 0 || constructor < 0 || parameter < 0) throw new Error(name + ' 原文档 owner 构造入口已变化');
      contents = contents.slice(0, parameter) + contents.slice(parameter).replace('private readonly _documents: ExtHostDocuments,',
        'private readonly _documents: ExtHostDocuments,\n    private readonly _notebooks: ExtHostNotebookController,');
    }
    contents = replaceOnce(contents, 'new CodeActionAdapter(this._documents,', 'new CodeActionAdapter(this._documents, this._notebooks,');
    contents = replaceOnce(contents, 'new RenameAdapter(this._documents,', 'new RenameAdapter(this._documents, this._notebooks,');
    contents = replaceOnce(contents, 'const commandsOrActions = await this._provider.provideCodeActions(doc, ran, codeActionContext, token);',
      'const versions = typeConvert.WorkspaceEdit.captureVersions(this._documents.getAllDocumentData(), this._notebooks.notebookDocuments);\n\t\tconst commandsOrActions = await this._provider.provideCodeActions(doc, ran, codeActionContext, token);');
    contents = replaceOnce(contents, 'typeConvert.WorkspaceEdit.from(toConvert.edit, undefined)', 'typeConvert.WorkspaceEdit.from(toConvert.edit, versions)');
    contents = replaceOnce(contents, 'const resolvedItem = (await this._provider.resolveCodeAction(item, token)) ?? item;',
      'const versions = typeConvert.WorkspaceEdit.captureVersions(this._documents.getAllDocumentData(), this._notebooks.notebookDocuments);\n\t\tconst resolvedItem = (await this._provider.resolveCodeAction(item, token)) ?? item;');
    contents = replaceOnce(contents, 'typeConvert.WorkspaceEdit.from(resolvedItem.edit, undefined)', 'typeConvert.WorkspaceEdit.from(resolvedItem.edit, versions)');
    contents = replaceOnce(contents, 'const value = await this._provider.provideRenameEdits(doc, pos, newName, token);',
      'const versions = typeConvert.WorkspaceEdit.captureVersions(this._documents.getAllDocumentData(), this._notebooks.notebookDocuments);\n\t\t\tconst value = await this._provider.provideRenameEdits(doc, pos, newName, token);');
    return replaceOnce(contents, 'return typeConvert.WorkspaceEdit.from(value);', 'return typeConvert.WorkspaceEdit.from(value, versions);');
  }
  if (path === paths[8]) {
    // 状态 token 会被本次文本修改取消，应用阶段只沿原 run 的父 token。
    let contents = replaceOnce(source, '\n\t\tthis._cts = new CancellationTokenSource();', '\n\t\tthis._cts = new CancellationTokenSource();\n    const operationToken = this._cts.token;');
    contents = replaceOnce(contents, '\n\t\t\tthis._bulkEditService.apply(renameResult, {', '\n\t\t\treturn this._bulkEditService.apply(renameResult, {');
    contents = replaceOnce(contents, 'showPreview: inputFieldResult.wantsPreview,', 'showPreview: inputFieldResult.wantsPreview,\n        token: operationToken,');
    return replaceOnce(contents, 'if (result.ariaSummary) {', 'if (result.isApplied && result.ariaSummary) {');
  }
  if (path === paths[9]) {
    let contents = replaceOnce(source, 'import { CancellationTokenSource }', 'import { CancellationToken, CancellationTokenSource }');
    contents = replaceOnce(contents, '\n\tpublic get requestUuid(): string {', '\n  public get token(): CancellationToken { return this._cancellationTokenSource.token; }\n\n\tpublic get requestUuid(): string {');
    contents = replaceOnce(contents, 'this._result.rejectReason || this._cancellationTokenSource.token.isCancellationRequested', 'this._result.rejectReason || this._result.lifecycleToken?.isCancellationRequested || this._cancellationTokenSource.token.isCancellationRequested');
    return replaceOnce(contents, 'bulkEditService.apply(workspaceEdit, { reason: source });', 'await bulkEditService.apply(workspaceEdit, { reason: source, token: renameRunnable.token });');
  }
  if (path === paths[11]) {
    let contents = `import { INotificationService } from '../../../../platform/notification/common/notification.js';\n` + source;
    contents = replaceOnce(contents, '@IKeybindingService private readonly _keybindingService: IKeybindingService,',
      '@IKeybindingService private readonly _keybindingService: IKeybindingService,\n\t\t@INotificationService private readonly _notificationService: INotificationService,');
    // 原 UI 应用 owner 统一呈现错误；通知保留原因并自行处理取消，不能触发全局启动失败。
    return replaceOnce(contents,
      'await this._instantiationService.invokeFunction(applyCodeAction, action, actionReason, { preview, editor: this._editor });\n\t\t} finally {',
      'await this._instantiationService.invokeFunction(applyCodeAction, action, actionReason, { preview, editor: this._editor });\n\t\t} catch (error) {\n\t\t\tthis._notificationService.error(error);\n\t\t} finally {');
  }
  if (path === paths[10]) {
    let contents = source;
    contents = replaceOnce(contents, 'export interface WorkspaceEdit {', 'export interface WorkspaceEdit {\n\t/** 仅窗口内保留结果所属注册生命周期，不进入扩展 RPC DTO。 */\n\t// TODO: 原 Chat response stream 分项移交后的应用仍须由其 owner 保留版本与生命周期。\n\treadonly lifecycleToken?: CancellationToken;');
    return replaceOnce(contents, 'export interface CodeActionProvider {', 'export interface CodeActionProvider {\n\treadonly lifecycleToken?: CancellationToken;');
  }
  if (path === paths[7]) {
    let contents = replaceOnce(source, 'import { CancellationToken }', 'import { CancellationToken, CancellationTokenSource }');
    // 原 item 已持有 provider；生命周期只在本次应用合并，不进入 action DTO 或第二份索引。
    contents = replaceOnce(contents, '\n\tconst bulkEditService = accessor.get(IBulkEditService);', `
  if (token.isCancellationRequested || item.provider?.lifecycleToken?.isCancellationRequested) return;
  const request = new CancellationTokenSource(token);
  const cancellation = item.provider?.lifecycleToken?.onCancellationRequested(() => request.cancel());
  try {
    token = request.token;
\tconst bulkEditService = accessor.get(IBulkEditService);`);
    contents = replaceOnce(contents, "\n\t\t\tcode: 'undoredo.codeAction',", "\n\t\t\tcode: 'undoredo.codeAction',\n      token,");
    contents = replaceOnce(contents, 'if (!result.isApplied) {', 'if (!result.isApplied || token.isCancellationRequested) {');
    return replaceOnce(contents, 'setTimeout(() => accessibilitySignalService.playSignal(AccessibilitySignal.codeActionApplied), CODE_ACTION_SOUND_APPLIED_DURATION);', `setTimeout(() => accessibilitySignalService.playSignal(AccessibilitySignal.codeActionApplied), CODE_ACTION_SOUND_APPLIED_DURATION);
  } finally { cancellation?.dispose(); request.dispose(); }`);
  }
  if (path !== paths[0]) return source;
  let contents = source;
  contents = replaceOnce(contents, 'import { CancellationToken }', 'import { CancellationToken, CancellationTokenSource }');
  contents = replaceOnce(contents, 'private readonly _proxy: ExtHostLanguageFeaturesShape;', `private _createProviderLifetime() {
    const lifetime = new CancellationTokenSource();
    const assertActive = (token: CancellationToken) => {
      if (lifetime.token.isCancellationRequested || token.isCancellationRequested) throw new CancellationError();
    };
    const invoke = async <T>(token: CancellationToken, operation: (token: CancellationToken) => Promise<T>): Promise<T> => {
      assertActive(token);
      const request = new CancellationTokenSource(token);
      const cancellation = lifetime.token.onCancellationRequested(() => request.cancel());
      try { return await operation(request.token); }
      finally { cancellation.dispose(); request.dispose(); }
    };
    return { lifetime, assertActive, invoke };
  }

  private readonly _proxy: ExtHostLanguageFeaturesShape;`);
  contents = replaceOnce(contents, 'const provider: languages.CodeActionProvider = {', `const { lifetime, assertActive, invoke } = this._createProviderLifetime();
    const provider: languages.CodeActionProvider = {
      lifecycleToken: lifetime.token,`);
  contents = replaceOnce(contents, `const listDto = await this._proxy.$provideCodeActions(handle, model.uri, rangeOrSelection, context, token);
\t\t\t\tif (!listDto) {
\t\t\t\t\treturn undefined;
\t\t\t\t}
\t\t\t\treturn {
\t\t\t\t\tactions: MainThreadLanguageFeatures._reviveCodeActionDto(listDto.actions, this._uriIdentService),
\t\t\t\t\tdispose: () => {
\t\t\t\t\t\tif (typeof listDto.cacheId === 'number') {
\t\t\t\t\t\t\tthis._proxy.$releaseCodeActions(handle, listDto.cacheId);
\t\t\t\t\t\t}
\t\t\t\t\t}
\t\t\t\t};`, `return invoke(token, async token => {
          const listDto = await this._proxy.$provideCodeActions(handle, model.uri, rangeOrSelection, context, token);
          const release = toDisposable(() => {
            if (typeof listDto?.cacheId === 'number') this._proxy.$releaseCodeActions(handle, listDto.cacheId);
          });
          try {
            assertActive(token);
            if (!listDto) return undefined;
            return { actions: MainThreadLanguageFeatures._reviveCodeActionDto(listDto.actions, this._uriIdentService), dispose: () => release.dispose() };
          } catch (error) { release.dispose(); throw error; }
        });`);
  contents = replaceOnce(contents, 'const resolved = await this._proxy.$resolveCodeAction(handle, (<ICodeActionDto>codeAction).cacheId!, token);', `return invoke(token, async token => {
        const resolved = await this._proxy.$resolveCodeAction(handle, (<ICodeActionDto>codeAction).cacheId!, token);
        assertActive(token);
        `);
  contents = replaceOnce(contents, '\n\t\t\t\treturn codeAction;\n\t\t\t};', '\n\t\t\t\treturn codeAction;\n      });\n\t\t\t};');
  contents = replaceOnce(contents, 'this._registrations.set(handle, this._languageFeaturesService.codeActionProvider.register(selector, provider));', `this._registrations.set(handle, combinedDisposable(
      this._languageFeaturesService.codeActionProvider.register(selector, provider),
      toDisposable(() => { lifetime.cancel(); lifetime.dispose(); })
    ));`);
  contents = replaceOnce(contents, `this._registrations.set(handle, this._languageFeaturesService.renameProvider.register(selector, {
\t\t\tprovideRenameEdits: (model: ITextModel, position: EditorPosition, newName: string, token: CancellationToken) => {
\t\t\t\treturn this._proxy.$provideRenameEdits(handle, model.uri, position, newName, token).then(data => reviveWorkspaceEditDto(data, this._uriIdentService));
\t\t\t},
\t\t\tresolveRenameLocation: supportResolveLocation
\t\t\t\t? (model: ITextModel, position: EditorPosition, token: CancellationToken): Promise<languages.RenameLocation | undefined> => this._proxy.$resolveRenameLocation(handle, model.uri, position, token)
\t\t\t\t: undefined
\t\t}));`, `const { lifetime, assertActive, invoke } = this._createProviderLifetime();
    this._registrations.set(handle, combinedDisposable(this._languageFeaturesService.renameProvider.register(selector, {
      provideRenameEdits: (model: ITextModel, position: EditorPosition, newName: string, token: CancellationToken) => invoke(token, async token => {
        const data = await this._proxy.$provideRenameEdits(handle, model.uri, position, newName, token);
        assertActive(token);
        const result = reviveWorkspaceEditDto(data, this._uriIdentService);
        // 不可枚举的本地字段不会随公开命令结果进入 RPC；预览和 inline 保留原结果。
        if (result) Object.defineProperty(result, 'lifecycleToken', { value: lifetime.token });
        return result;
      }),
      resolveRenameLocation: supportResolveLocation
        ? (model: ITextModel, position: EditorPosition, token: CancellationToken) => invoke(token, async token => {
          const location = await this._proxy.$resolveRenameLocation(handle, model.uri, position, token);
          assertActive(token);
          return location;
        }) : undefined,
    }), toDisposable(() => { lifetime.cancel(); lifetime.dispose(); })));`);
  return contents;
}
