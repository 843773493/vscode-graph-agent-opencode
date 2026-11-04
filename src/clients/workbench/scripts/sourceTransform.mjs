import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { searchEditorSourcePaths, transformSearchEditor } from './searchEditorTransform.mjs';

export const pinnedUpstreamCommit = 'fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca';
export const transformedSourcePaths = [
  'src/vs/workbench/browser/web.main.ts',
  'src/vs/workbench/browser/quickaccess.ts',
  'src/vs/platform/quickinput/browser/quickAccess.ts',
  'src/vs/platform/quickinput/browser/helpQuickAccess.ts',
  'src/vs/workbench/services/search/common/queryBuilder.ts',
  'src/vs/workbench/contrib/search/browser/search.contribution.ts',
  'src/vs/workbench/contrib/search/browser/searchQuickAccess.contribution.ts',
  'src/vs/workbench/contrib/search/browser/quickTextSearch/textSearchQuickAccess.ts',
  'src/vs/workbench/contrib/search/browser/searchTreeModel/searchModel.ts',
  'src/vs/workbench/contrib/search/browser/searchTreeModel/match.ts',
  'src/vs/workbench/contrib/search/browser/searchView.ts',
  'src/vs/workbench/browser/parts/views/viewPaneContainer.ts',
  'src/vs/workbench/contrib/files/browser/editors/fileEditorInput.ts',
  'src/vs/workbench/contrib/mediaPreview/browser/imagePreviewEditor.ts',
  ...searchEditorSourcePaths,
];

function replaceOnce(source, marker, replacement) {
  if (source.split(marker).length !== 2) throw new Error(`上游转换锚点必须恰好一次：${marker}`);
  return source.replace(marker, () => replacement);
}

/** 构建与 Vite 共用上游源码定制，不改写固定子模块。 */
export function sourceTransform(root, productConfiguration, extensions) {
  if (productConfiguration.commit !== pinnedUpstreamCommit) throw new Error(`上游版本不匹配：要求 ${pinnedUpstreamCommit}`);
  const upstream = join(root, 'upstream');
  return async function transformSource(path, source) {
    let contents = source;
    const replacements = {
      '/*BUILD->INSERT_PRODUCT_CONFIGURATION*/': JSON.stringify(productConfiguration).slice(1, -1),
      '/*BUILD->INSERT_BUILTIN_EXTENSIONS*/': JSON.stringify(extensions).slice(1, -1),
    };
    for (const [marker, value] of Object.entries(replacements)) contents = contents.replace(marker, () => value);
    if (path === join(upstream, 'src/vs/workbench/browser/web.main.ts')) {
      const marker = 'await this.registerIndexedDBFileSystemProviders(environmentService, fileService, logService, loggerService, logsPath);';
      const cacheSchemes = '[Schemas.file, Schemas.vscodeUserData, Schemas.tmp] /* Cache all non native resources */';
      const closeDatabase = 'this.onWillShutdownDisposables.add(toDisposable(() => indexedDB?.close()));';
      // 默认虚拟工作区与 vscode-userdata 一样由本地 IndexedDB 提供；额外配置缓存会留下关闭后的异步写入。
      contents = replaceOnce(contents, marker, `${marker}\nawait installWorkspaceFileSystem(fileService);`);
      contents = replaceOnce(contents, cacheSchemes, '[Schemas.file, Schemas.vscodeUserData, Schemas.tmp, workspaceFolder.scheme]');
      // pagehide 的同步 shutdown 仍会产生延迟日志；数据库连接由浏览器随文档释放，不能提前关闭并中止事务。
      contents = replaceOnce(contents, closeDatabase, '// IndexedDB 连接随浏览器文档释放，保留关闭期间日志和设置写入的机会。');
      contents = `import { installWorkspaceFileSystem, workspaceFolder } from ${JSON.stringify(join(root, 'src/filesystem.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/mediaPreview/browser/imagePreviewEditor.ts')) {
      // 业务文件删除及读取失败不能继续把旧图当作当前成功；其余原生文件沿原版行为。
      contents = replaceOnce(contents, 'if (e.contains(input.resource, FileChangeType.UPDATED, FileChangeType.ADDED)) {',
        "if (e.contains(input.resource, FileChangeType.UPDATED, FileChangeType.ADDED) || input.resource.scheme === 'boxteam-file' && e.contains(input.resource, FileChangeType.DELETED)) {");
      const swallowed = `} catch {
\t\t\t// Keep showing the previous image, e.g. when the file is in the middle of being written
\t\t\treturn;
\t\t}`;
      contents = replaceOnce(contents, swallowed, `} catch (error) {
if (input.resource.scheme === 'boxteam-file' && this._isCurrentLoad(loadId, input)) {
  this._viewer?.clear();
  this._objectUrl.clear();
  this._fileSize = undefined;
  this._setMessage(localize('imagePreview.readFailed', "Unable to read image: {0}", error instanceof Error ? error.message : String(error)));
  this._onDidChangeImageInfo.fire();
}
return;
}`);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/files/browser/editors/fileEditorInput.ts')) {
      const reference = 'private cachedTextFileModelReference: IReference<ITextFileEditorModel> | undefined = undefined;';
      contents = replaceOnce(contents, reference, `${reference}
private pendingTextFileModelReference: Promise<IReference<ITextFileEditorModel>> | undefined;`);
      const acquire = `if (!this.cachedTextFileModelReference) {
\t\t\t\tthis.cachedTextFileModelReference = await this.textModelService.createModelReference(this.resource) as IReference<ITextFileEditorModel>;
\t\t\t}

\t\t\tconst model = this.cachedTextFileModelReference.object;`;
      // 分屏并发解析共用同一在途引用，只有取得引用的 Promise 写入缓存；关闭时仍由原 input 释放。
      contents = replaceOnce(contents, acquire, `const reference = this.cachedTextFileModelReference ?? await (this.pendingTextFileModelReference ??=
this.textModelService.createModelReference(this.resource).then(reference => {
  this.cachedTextFileModelReference = reference as IReference<ITextFileEditorModel>;
  return this.cachedTextFileModelReference;
}).finally(() => { this.pendingTextFileModelReference = undefined; }));
const model = reference.object;`);
    }
    if (path === join(upstream, 'src/vs/workbench/services/search/common/queryBuilder.ts')) {
      const commonQuery = 'private commonQuery(folderResources: (IWorkspaceFolderData | URI)[] = [], options: ICommonQueryBuilderOptions = {}): ICommonQueryProps<uri> {';
      contents = replaceOnce(contents, commonQuery, `${commonQuery}\noptions = scopeQueryOptions(this.workspaceContextService, folderResources, options);`);
      const queryProps = 'const queryProps: ICommonQueryProps<uri> = {';
      contents = replaceOnce(contents, queryProps, `assertSearchFolders(this.workspaceContextService, folderQueries);\n${queryProps}`);
      // 业务 Provider 按完整 URI 选精确已打开文件，避免原 fsPath 分支重建为其它 authority 的根。
      contents = replaceOnce(contents, 'if (options.onlyOpenEditors) {', 'if (options.onlyOpenEditors && !scopedSearch(this.workspaceContextService)) {');
      contents = `import { scopeQueryOptions, assertSearchFolders, scopedSearch } from ${JSON.stringify(join(root, 'src/search/scope.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/browser/quickaccess.ts')) {
      // hide 会释放 Provider；异步关闭前先消费本次状态，不能在 await 后读取已 reset 的字段。
      contents = replaceOnce(contents, '\tasync restore(): Promise<void> {\n\t\tif (this.editorViewState) {\n\t\t\tfor (const editor of this.openedTransientEditors) {',
        '\tasync restore(): Promise<void> {\n\t\tconst editorViewState = this.editorViewState;\n\t\tconst openedTransientEditors = [...this.openedTransientEditors];\n\t\tthis.reset();\n\t\tif (editorViewState) {\n\t\t\tfor (const editor of openedTransientEditors) {');
      contents = replaceOnce(contents, '\t\t\tawait this.editorViewState.group.openEditor(this.editorViewState.editor, {\n\t\t\t\tviewState: this.editorViewState.state,',
        '\t\t\tawait editorViewState.group.openEditor(editorViewState.editor, {\n\t\t\t\tviewState: editorViewState.state,');
      contents = replaceOnce(contents, '\n\t\t\tthis.reset();\n\t\t}\n\t}\n\n\treset()', '\n\t\t}\n\t}\n\n\treset()');
    }
    if (path === join(upstream, 'src/vs/platform/quickinput/browser/quickAccess.ts')) {
      contents = replaceOnce(contents, '@IContextKeyService private readonly contextKeyService: IContextKeyService',
        '@IContextKeyService private readonly contextKeyService: IContextKeyService,\n@IEditorService private readonly editors: IEditorService');
      const entry = 'private doShowOrPick(value: string, pick: boolean, options?: IQuickAccessOptions): Promise<IQuickPickItem[] | undefined> | void {';
      contents = replaceOnce(contents, entry, `${entry}\noptions = prepareSearchInvocation(options, this.editors);`);
      contents = `import { prepareSearchInvocation } from ${JSON.stringify(join(root, 'src/search/target.js'))};\nimport { IEditorService } from ${JSON.stringify(join(root, 'upstream/src/vs/workbench/services/editor/common/editorService.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/platform/quickinput/browser/helpQuickAccess.ts')) {
      contents = replaceOnce(contents, 'provide(picker: IQuickPick<IHelpQuickAccessPickItem, { useSeparators: true }>): IDisposable {',
        'provide(picker: IQuickPick<IHelpQuickAccessPickItem, { useSeparators: true }>, _token: CancellationToken, options?: AnythingQuickAccessProviderRunOptions): IDisposable {');
      contents = replaceOnce(contents, 'this.quickInputService.quickAccess.show(item.prefix, { preserveValue: true });',
        'const providerOptions: AnythingQuickAccessProviderRunOptions = { ...options, includeHelp: item.prefix ? options?.includeHelp : false };\nthis.quickInputService.quickAccess.show(item.prefix, { preserveValue: true, providerOptions });');
      contents = replaceOnce(contents, 'this.quickInputService.quickAccess.show(providerDescriptor.prefix, { preserveValue: true });',
        'this.quickInputService.quickAccess.show(providerDescriptor.prefix, { preserveValue: true, providerOptions: options });');
      contents = `import type { CancellationToken } from '../../../base/common/cancellation.js';\nimport type { AnythingQuickAccessProviderRunOptions } from '../common/quickAccess.js';\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/search.contribution.ts')) {
      contents = replaceOnce(contents, 'ctorDescriptor: new SyncDescriptor(SearchView),', 'ctorDescriptor: new SyncDescriptor(ScopedSearchView),');
      contents = replaceOnce(contents, "import { SearchView } from './searchView.js';",
        `import { ScopedSearchView } from ${JSON.stringify(join(root, 'src/search/searchView.js'))};`);
      contents = replaceOnce(contents, 'quickAccessRegistry.registerQuickAccessProvider({', 'quickAccessRegistry.registerQuickAccessProvider(selectTextSearchDescriptor({');
      contents = replaceOnce(contents, '\n});\n\n// Configuration', '\n}));\n\n// Configuration');
      contents = `import { selectTextSearchDescriptor } from ${JSON.stringify(join(root, 'src/search/textQuickAccess.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchQuickAccess.contribution.ts')) {
      contents = replaceOnce(contents, 'quickAccessRegistry.registerQuickAccessProvider({\n\tctor: AnythingQuickAccessProvider,',
        'quickAccessRegistry.registerQuickAccessProvider(selectDefaultSearchDescriptor({\n\tctor: AnythingQuickAccessProvider,');
      contents = replaceOnce(contents, '\n});\n\nquickAccessRegistry.registerQuickAccessProvider({\n\tctor: SymbolsQuickAccessProvider,',
        '\n}));\n\nquickAccessRegistry.registerQuickAccessProvider({\n\tctor: SymbolsQuickAccessProvider,');
      contents = replaceOnce(contents, 'quickAccessRegistry.registerQuickAccessProvider({\n\tctor: SymbolsQuickAccessProvider,',
        'quickAccessRegistry.registerQuickAccessProvider(selectSymbolSearchDescriptor({\n\tctor: SymbolsQuickAccessProvider,');
      contents = replaceOnce(contents, "commandId: 'workbench.action.showAllSymbols' }]\n});", "commandId: 'workbench.action.showAllSymbols' }]\n}));");
      contents = `import { selectDefaultSearchDescriptor } from ${JSON.stringify(join(root, 'src/search/quickAccess.js'))};\nimport { selectSymbolSearchDescriptor } from ${JSON.stringify(join(root, 'src/search/symbolsQuickAccess.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/browser/parts/views/viewPaneContainer.ts')) {
      contents = replaceOnce(contents, 'return this.instantiationService.createInstance(viewDescriptor.ctorDescriptor.ctor, ...(viewDescriptor.ctorDescriptor.staticArguments || []), options);',
        'return createSearchView(this.instantiationService, viewDescriptor, options);');
      contents = `import { createSearchView } from ${JSON.stringify(join(root, 'src/search/viewFactory.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchView.ts')) {
      contents = replaceOnce(contents, '@IWorkspaceContextService private readonly contextService: IWorkspaceContextService,',
        '@IWorkspaceContextService protected readonly contextService: IWorkspaceContextService,');
      contents = replaceOnce(contents, 'private pauseSearching = false;', 'protected pauseSearching = false;');
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchTreeModel/searchModel.ts')) {
      const declaration = 'export class SearchModelImpl extends Disposable implements ISearchModel {';
      contents = replaceOnce(contents, declaration, `${declaration}\npublic ownSearchContext(context: IDisposable): void { this._register(context); }`);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchTreeModel/match.ts')) {
      // lcut 允许整词超出长度；有界片段的长前缀不能耗尽额度并隐藏真正命中。
      const before = "const fullBefore = this._oneLinePreviewText.substring(0, this._rangeInPreviewText.startColumn - 1), before = lcut(fullBefore, 26, '…');";
      contents = replaceOnce(contents, before, `const fullBefore = this._oneLinePreviewText.substring(0, this._rangeInPreviewText.startColumn - 1);
let before = lcut(fullBefore, 26, '…');
if (before.length > 26) before = '…' + Array.from(before).slice(-26).join('');`);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/quickTextSearch/textSearchQuickAccess.ts')) {
      // 由原 QueryBuilder 消费用户的 search.useIgnoreFiles，交目标文件 owner 解释规则。
      contents = replaceOnce(contents, '\tdisregardIgnoreFiles: false,\n', '');
      const transfer = `\t\tthis._viewsService.openView(VIEW_ID, false);
\t\tconst viewlet: SearchView | undefined = this._viewsService.getActiveViewWithId(VIEW_ID) as SearchView;
\t\tawait viewlet.replaceSearchModel(this.searchModel, this.currentAsyncSearch);

\t\tthis.searchModel = this._instantiationService.createInstance(SearchModelImpl);
\t\tthis.searchModel.location = SearchModelLocation.QUICK_ACCESS;`;
      contents = replaceOnce(contents, transfer, `const transferredModel = this.searchModel;
this._store.deleteAndLeak(transferredModel);
transferSearchContext(this._contextService, transferredModel);
const asyncSearch = this.currentAsyncSearch;
this.searchModel = this._register(this._instantiationService.createInstance(SearchModelImpl));
this.searchModel.location = SearchModelLocation.QUICK_ACCESS;
let viewlet: SearchView;
try {
  const view = await this._viewsService.openView<SearchView>(VIEW_ID, false);
  if (!view) throw new Error('原搜索视图不可用');
  viewlet = view;
  await viewlet.replaceSearchModel(transferredModel, asyncSearch);
} catch (error) {
  transferredModel.dispose();
  throw error;
}`);
      contents = `import { transferSearchContext } from ${JSON.stringify(join(root, 'src/search/scope.js'))};\n` + contents;
    }
    return transformSearchEditor(root, path, contents, replaceOnce);
  };
}

export function buildSourcePlugin(root, transformSource) {
  return {
    name: 'upstream-build-configuration',
    setup(builder) {
      builder.onResolve({ filter: /\.css\?text$/ }, ({ path, resolveDir }) => ({ path: resolve(resolveDir, path.slice(0, -5)), namespace: 'session-stylesheet' }));
      builder.onLoad({ filter: /.*/, namespace: 'session-stylesheet' }, async ({ path }) => ({ contents: await readFile(path, 'utf8'), loader: 'text' }));
      builder.onResolve({ filter: /^\.\/codicon\.ttf$/ }, () => ({ path: join(root, 'node_modules/@vscode/codicons/dist/codicon.ttf') }));
      builder.onLoad({ filter: /\.ts$/ }, async ({ path }) => {
        if (path.endsWith('.d.ts')) return;
        const source = await readFile(path, 'utf8');
        const contents = await transformSource(path, source);
        if (contents !== source) return { contents, loader: 'ts' };
      });
      builder.onResolve({ filter: /node_modules\// }, ({ path }) => ({ path: join(root, 'node_modules', path.split('node_modules/').at(-1)) }));
    },
  };
}
