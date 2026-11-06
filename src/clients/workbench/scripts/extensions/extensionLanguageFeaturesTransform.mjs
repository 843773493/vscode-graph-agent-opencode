import { join } from 'node:path';
import ts from 'typescript';

export const extensionLanguageFeaturesSourcePaths = [
  'src/vs/editor/common/languageFeatureRegistry.ts',
  'src/vs/editor/common/languageSelector.ts',
  'src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts',
  'src/vs/editor/contrib/wordHighlighter/browser/wordHighlighter.ts',
  'src/vs/editor/common/languages.ts',
  'src/vs/editor/contrib/wordHighlighter/browser/textualHighlightProvider.ts',
  'src/vs/editor/contrib/codeAction/browser/codeAction.ts',
  'src/vs/workbench/api/common/extHostLanguageFeatures.ts',
  'src/vs/editor/contrib/codeAction/browser/codeActionController.ts',
];

export function transformExtensionLanguageFeatures(root, path, source, replaceOnce, runtime = 'browser') {
  const paths = extensionLanguageFeaturesSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[7]) {
    let contents = `import { ResourceMap } from '../../../base/common/map.js';\n` + source;
    contents = replaceOnce(contents, '// --- adapter', `function captureWorkspaceEditVersions(documents: ExtHostDocuments): typeConvert.WorkspaceEdit.IVersionInformationProvider {
    // 只冻结本次调用已同步的版本号，不能给等待期间产生的旧编辑贴上返回时的新版本。
    const versions = new ResourceMap<number>();
    for (const data of documents.getAllDocumentData()) versions.set(data.document.uri, data.version);
    return {
      getTextDocumentVersion: uri => versions.get(uri),
      // TODO: Notebook 版本生产仍须接通原 Notebook 文档 owner。
      getNotebookDocumentVersion: () => undefined,
    };
}

// --- adapter
`);
    contents = replaceOnce(contents, 'const commandsOrActions = await this._provider.provideCodeActions(doc, ran, codeActionContext, token);',
      'const versions = captureWorkspaceEditVersions(this._documents);\n\t\tconst commandsOrActions = await this._provider.provideCodeActions(doc, ran, codeActionContext, token);');
    contents = replaceOnce(contents, 'typeConvert.WorkspaceEdit.from(toConvert.edit, undefined)', 'typeConvert.WorkspaceEdit.from(toConvert.edit, versions)');
    contents = replaceOnce(contents, 'const resolvedItem = (await this._provider.resolveCodeAction(item, token)) ?? item;',
      'const versions = captureWorkspaceEditVersions(this._documents);\n\t\tconst resolvedItem = (await this._provider.resolveCodeAction(item, token)) ?? item;');
    contents = replaceOnce(contents, 'typeConvert.WorkspaceEdit.from(resolvedItem.edit, undefined)', 'typeConvert.WorkspaceEdit.from(resolvedItem.edit, versions)');
    contents = replaceOnce(contents, 'const value = await this._provider.provideRenameEdits(doc, pos, newName, token);',
      'const versions = captureWorkspaceEditVersions(this._documents);\n\t\t\tconst value = await this._provider.provideRenameEdits(doc, pos, newName, token);');
    return replaceOnce(contents, 'return typeConvert.WorkspaceEdit.from(value);', 'return typeConvert.WorkspaceEdit.from(value, versions);');
  }
  if (runtime === 'node') return source;
  if (path === paths[8]) {
    let contents = `import { INotificationService } from '../../../../platform/notification/common/notification.js';\n` + source;
    contents = replaceOnce(contents, '@IKeybindingService private readonly _keybindingService: IKeybindingService,',
      '@IKeybindingService private readonly _keybindingService: IKeybindingService,\n\t\t@INotificationService private readonly _notificationService: INotificationService,');
    // 原 UI 应用 owner 统一呈现错误；通知保留原因并自行处理取消，不能触发全局启动失败。
    return replaceOnce(contents,
      'await this._instantiationService.invokeFunction(applyCodeAction, action, actionReason, { preview, editor: this._editor });\n\t\t} finally {',
      'await this._instantiationService.invokeFunction(applyCodeAction, action, actionReason, { preview, editor: this._editor });\n\t\t} catch (error) {\n\t\t\tthis._notificationService.error(error);\n\t\t} finally {');
  }
  if (path === paths[0]) {
    let contents = replaceOnce(source, 'readonly provider: T;', 'readonly provider: T;\n\treadonly selectorScore: typeof score;');
    contents = replaceOnce(contents, 'register(selector: LanguageSelector, provider: T): IDisposable {', 'register(selector: LanguageSelector, provider: T, selectorScore: typeof score = score): IDisposable {');
    contents = replaceOnce(contents, '\n\t\t\tprovider,\n', '\n\t\t\tprovider,\n\t\t\tselectorScore,\n');
    contents = replaceOnce(contents, '\n\tallNoModel(): T[] {', `
  /** 次要文档也消费同一注册项的评分，不能另存 provider.selector 绕过宿主归属。 */
  score(provider: T, model: ITextModel): number {
    const entry = this._entries.find(entry => entry.provider === provider);
    if (!entry) return 0;
    const notebook = this._notebookInfoResolver?.(model.uri);
    return entry.selectorScore(entry.selector, model.uri, model.getLanguageId(), shouldSynchronizeModel(model), notebook?.uri, notebook?.type);
  }

\tallNoModel(): T[] {`);
    return replaceOnce(contents, 'entry._score = score(entry.selector,', 'entry._score = entry.selectorScore(entry.selector,');
  }
  if (path === paths[1]) {
    let contents = replaceOnce(source, 'candidateNotebookType: string | undefined): number {', 'candidateNotebookType: string | undefined, patternMatcher?: (pattern: string | IRelativePattern, resource: URI) => boolean): number {');
    contents = replaceOnce(contents, 'candidateIsSynchronized, candidateNotebookUri, candidateNotebookType);', 'candidateIsSynchronized, candidateNotebookUri, candidateNotebookType, patternMatcher);');
    return replaceOnce(contents, '\n\t\tif (pattern) {', '\n\t\tif (pattern) {\n      if (patternMatcher) return patternMatcher(pattern, candidateUri) ? 10 : 0;');
  }
  if (path === paths[3]) {
    let contents = replaceOnce(source, "import { score } from '../../../common/languageSelector.js';\n", '');
    contents = replaceOnce(contents, 'score(provider.selector, otherModel.uri, otherModel.getLanguageId(), true, undefined, undefined)', 'registry.score(provider, otherModel)');
    return contents;
  }
  if (path === paths[4]) {
    let contents = replaceOnce(source, "import { LanguageSelector } from './languageSelector.js';\n", '');
    contents = replaceOnce(contents, '\n\treadonly selector: LanguageSelector;\n', '');
    return replaceOnce(contents, 'export interface CodeActionProvider {', 'export interface CodeActionProvider {\n\treadonly lifecycleToken?: CancellationToken;');
  }
  if (path === paths[5]) {
    let contents = replaceOnce(source, "import { LanguageFilter } from '../../../common/languageSelector.js';\n", '');
    return replaceOnce(contents, "\n\tselector: LanguageFilter = { language: '*' };\n", '');
  }
  if (path === paths[6]) {
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
  if (path !== paths[2]) return source;
  let contents = `import { assertHostWorkspaceEdit } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};
import { hostLanguageFeatureScore } from ${JSON.stringify(join(root, 'src/extensions/languageFeatures.js'))};
import type { score } from '../../../editor/common/languageSelector.js';\n` + source;
  contents = replaceOnce(contents, 'import { CancellationToken }', 'import { CancellationToken, CancellationTokenSource }');
  contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
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
  contents = replaceOnce(contents, 'private static _reviveCodeActionDto(data: ReadonlyArray<ICodeActionDto>, uriIdentService: IUriIdentityService)', 'private _reviveCodeActionDto(data: ReadonlyArray<ICodeActionDto>)');
  contents = replaceOnce(contents, 'data?.forEach(code => reviveWorkspaceEditDto(code.edit, uriIdentService));', `data?.forEach(code => { if (code.edit) assertHostWorkspaceEdit(this.extHostContext.extensionHost, code.edit); });
    data?.forEach(code => reviveWorkspaceEditDto(code.edit, this._uriIdentService));`);
  contents = replaceOnce(contents, 'const provider: languages.CodeActionProvider = {', `const { lifetime, assertActive, invoke } = this._createProviderLifetime();
    const provider: languages.CodeActionProvider = {
      lifecycleToken: this.extHostContext.extensionHost.binding ? lifetime.token : undefined,`);
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
            return { actions: this._reviveCodeActionDto(listDto.actions), dispose: () => release.dispose() };
          } catch (error) { release.dispose(); throw error; }
        });`);
  contents = replaceOnce(contents, 'const resolved = await this._proxy.$resolveCodeAction(handle, (<ICodeActionDto>codeAction).cacheId!, token);', `return invoke(token, async token => {
        const resolved = await this._proxy.$resolveCodeAction(handle, (<ICodeActionDto>codeAction).cacheId!, token);
        assertActive(token);
        if (resolved.edit) assertHostWorkspaceEdit(this.extHostContext.extensionHost, resolved.edit);`);
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
    // TODO: 已返回编辑在 RenameController 预览及 inline rename 中的应用取消须沿各原 owner 继续接通。
    this._registrations.set(handle, combinedDisposable(this._languageFeaturesService.renameProvider.register(selector, {
      provideRenameEdits: (model: ITextModel, position: EditorPosition, newName: string, token: CancellationToken) => invoke(token, async token => {
        const data = await this._proxy.$provideRenameEdits(handle, model.uri, position, newName, token);
        assertActive(token);
        // 原拒绝结果允许 edits 为 undefined；只校验实际编辑，不改写 rejectReason。
        if (data?.edits) assertHostWorkspaceEdit(this.extHostContext.extensionHost, data);
        return reviveWorkspaceEditDto(data, this._uriIdentService);
      }),
      resolveRenameLocation: supportResolveLocation
        ? (model: ITextModel, position: EditorPosition, token: CancellationToken) => invoke(token, async token => {
          const location = await this._proxy.$resolveRenameLocation(handle, model.uri, position, token);
          assertActive(token);
          return location;
        }) : undefined,
    }), toDisposable(() => { lifetime.cancel(); lifetime.dispose(); })));`);
  contents = replaceOnce(contents, 'private readonly _selector: IDocumentFilterDto[],', 'private readonly _selector: IDocumentFilterDto[],\n\t\tprivate readonly _selectorScore: typeof score | undefined,');
  contents = replaceOnce(contents, '\n\t\t\tselector,\n\t\t\tthis._proxy,', '\n\t\t\tselector,\n\t\t\thostLanguageFeatureScore(this.extHostContext.extensionHost, selector),\n\t\t\tthis._proxy,');
  contents = replaceOnce(contents, '\n\t\t\tselector: selector,\n', '\n');
  // 用现有 TypeScript parser 定位原调用的末参，不复制各 Provider 或手写括号解析。
  const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true);
  const insertions = [];
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'register') {
      const first = node.arguments[0]?.getText(parsed);
      if (first === 'selector' || first === 'this._selector') {
        if (node.arguments.length !== 2) throw new Error('原语言 Provider 注册参数已变化');
        insertions.push({ position: node.arguments[1].end, text: first === 'selector'
          ? ', hostLanguageFeatureScore(this.extHostContext.extensionHost, selector)' : ', this._selectorScore' });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  if (insertions.length !== 33) throw new Error('原语言 Provider 注册清单已变化：' + insertions.length);
  for (const { position, text } of insertions.sort((a, b) => b.position - a.position)) contents = contents.slice(0, position) + text + contents.slice(position);
  return contents;
}
