import { join } from 'node:path';

export const searchEditorSourcePaths = [
  'src/vs/workbench/contrib/searchEditor/browser/constants.ts',
  'src/vs/workbench/contrib/searchEditor/browser/searchEditorInput.ts',
  'src/vs/workbench/contrib/searchEditor/browser/searchEditorSerialization.ts',
  'src/vs/workbench/contrib/searchEditor/browser/searchEditorActions.ts',
  'src/vs/workbench/contrib/searchEditor/browser/searchEditor.contribution.ts',
  'src/vs/workbench/contrib/searchEditor/browser/searchEditor.ts',
  'extensions/search-result/src/extension.ts',
];

/** 仍由唯一 sourceTransform 调用；只提取原编辑器的 typed 接线点。 */
export function transformSearchEditor(root, path, source, replaceOnce) {
  const upstream = join(root, 'upstream');
  const matches = relative => path === join(upstream, relative);
  const scopeImport = names => `import { ${names} } from ${JSON.stringify(join(root, 'src/search/editorScope.js'))};\n`;
  let contents = source;
  if (matches(searchEditorSourcePaths[0])) {
    contents = `import type { SearchEditorScope } from ${JSON.stringify(join(root, 'src/search/editorScope.js'))};\n` + contents;
    contents = replaceOnce(contents, 'export type SearchConfiguration = {', 'export type SearchConfiguration = {\n scope?: SearchEditorScope;');
  }
  if (matches(searchEditorSourcePaths[1])) {
    contents = scopeImport('parseSearchEditorScope') + contents;
    // 原结果模型必须由 Input 持有解析器引用，词语高亮等临时消费者才能共享同一生命周期。
    contents = `import { MutableDisposable, type IReference } from '../../../../base/common/lifecycle.js';
import { ITextModelService, type IResolvedTextEditorModel } from '../../../../editor/common/services/resolverService.js';\n` + contents;
    contents = replaceOnce(contents, "import { IModelService } from '../../../../editor/common/services/model.js';\n", '');
    contents = replaceOnce(contents, '@IModelService private readonly modelService: IModelService,', '@ITextModelService private readonly textModelService: ITextModelService,');
    contents = replaceOnce(contents, 'private _cachedResultsModel: ITextModel | undefined;', `private readonly modelReference = this._register(new MutableDisposable<IReference<IResolvedTextEditorModel>>());
private pendingModel: ReturnType<SearchEditorModel['resolve']> | undefined;
private _cachedResultsModel: ITextModel | undefined;`);
    contents = replaceOnce(contents, 'return this.model.resolve().then(data => {', `this.pendingModel ??= this.model.resolve().then(async data => {
this.modelReference.value = await this.textModelService.createModelReference(this.modelUri);
return data;
});
return this.pendingModel.then(data => {`);
    contents = replaceOnce(contents, '\t\tthis.modelService.destroyModel(this.modelUri);\n', '');
    contents = replaceOnce(contents, 'const priorConfig = reuseOldSettings', 'const priorConfig: Partial<SearchConfiguration> = reuseOldSettings');
    contents = replaceOnce(contents, 'const config = { ...defaultConfig, ...priorConfig, ...existingData.config };',
      'const { scope: _priorScope, ...priorOptions } = priorConfig;\nconst config = { ...defaultConfig, ...priorOptions, ...existingData.config };\nconfig.scope = parseSearchEditorScope(config.scope);');
    contents = replaceOnce(contents, 'public tryReadConfigSync(): SearchConfiguration | undefined {', `public updateSearchConfig(config: Partial<SearchConfiguration>): void {
const model = this._cachedConfigurationModel;
if (!model) throw new Error('搜索编辑器配置尚未加载');
if (config !== model.config) model.updateConfig({ ...model.config, ...config, scope: parseSearchEditorScope(config.scope ?? model.config.scope) });
}
public tryReadConfigSync(): SearchConfiguration | undefined {`);
    // 原 rawData 已正式接受 modelUri，无须上游的 any 逃逸。
    contents = replaceOnce(contents, `\t\t\t// eslint-disable-next-line local/code-no-any-casts
\t\t\t{ from: 'rawData', config, resultsContents: results, modelUri: newModelUri } as any // modelUri is not in the type, but we handle it below`,
      "\t\t\t{ from: 'rawData', config, resultsContents: results, modelUri: newModelUri }");
  }
  if (matches(searchEditorSourcePaths[2])) {
    contents = scopeImport('parseSearchEditorScope, searchEditorScopeFromQuery, searchEditorHeaderLines') + contents;
    contents = replaceOnce(contents, 'query: pattern.contentPattern.pattern,', 'scope: searchEditorScopeFromQuery(pattern),\nquery: pattern.contentPattern.pattern,');
    contents = replaceOnce(contents, '`# Query: ${escapeNewlines(config.query ?? \'\')}`,',
      '`# Query: ${escapeNewlines(config.query ?? \'\')}`,\nconfig.scope && `# BoxTeamScope: ${JSON.stringify(parseSearchEditorScope(config.scope))}`,');
    contents = replaceOnce(contents, '(config.isCaseSensitive || config.matchWholeWord || config.isRegexp || config.useExcludeSettingsAndIgnoreFiles === false)',
      '(config.isCaseSensitive || config.matchWholeWord || config.isRegexp || config.onlyOpenEditors || config.useExcludeSettingsAndIgnoreFiles === false)');
    contents = replaceOnce(contents, 'extractSearchQueryFromLines(model.getValueInRange(new Range(1, 1, 6, 1)).split(lineDelimiter));',
      'extractSearchQueryFromLines(searchEditorHeaderLines(model));');
    contents = replaceOnce(contents, "case 'Query': query.query = unescapeNewlines(value); break;",
      "case 'BoxTeamScope': query.scope = parseSearchEditorScope(JSON.parse(value)); break;\ncase 'Query': query.query = unescapeNewlines(value); break;");
  }
  if (matches(searchEditorSourcePaths[3])) {
    contents = scopeImport('searchEditorScopeKey, searchEditorScopeFromQuery') + contents;
    contents = `import { captureNewSearchEditorScope } from ${JSON.stringify(join(root, 'src/search/searchEditor.js'))};\n` + contents;
    contents = replaceOnce(contents, 'await instantiationService.invokeFunction(openNewSearchEditor, {\n\t\t\tfilesToInclude:',
      'await instantiationService.invokeFunction(openNewSearchEditor, {\nscope: searchView.searchResult.query ? searchEditorScopeFromQuery(searchView.searchResult.query) : undefined,\n\t\t\tfilesToInclude:');
    contents = replaceOnce(contents, "const args: OpenSearchEditorArgs = { query: seedSearchStringFromSelection ? selected : undefined };",
      "const args: OpenSearchEditorArgs = { query: seedSearchStringFromSelection ? selected : undefined, scope: await captureNewSearchEditorScope(editorService, _args.scope) };");
    contents = replaceOnce(contents, 'find(id => id.editor.typeId === SearchEditorInput.ID)',
      'find(id => id.editor instanceof SearchEditorInput && searchEditorScopeKey(id.editor.tryReadConfigSync()?.scope) === searchEditorScopeKey(args.scope))');
    contents = replaceOnce(contents, 'config.onlyOpenEditors = onlySearchInOpenEditors;',
      'config.onlyOpenEditors = onlySearchInOpenEditors;\nconfig.scope = searchEditorScopeFromQuery(searchResult.query);');
    contents = replaceOnce(contents, 'const labelFormatter = (uri: URI): string => labelService.getUriLabel(uri, { relative: true });',
      'const labelFormatter = (uri: URI): string => uri.toString();');
    contents = replaceOnce(contents, '\t\tconst labelService = accessor.get(ILabelService);\n', '');
    contents = replaceOnce(contents, "import { ILabelService } from '../../../../platform/label/common/label.js';\n", '');
  }
  if (matches(searchEditorSourcePaths[4])) {
    contents = `import { ScopedSearchEditor } from ${JSON.stringify(join(root, 'src/search/searchEditor.js'))};\n` + contents;
    contents = replaceOnce(contents, '\n\t\tSearchEditor,\n\t\tSearchEditor.ID,', '\n\t\tScopedSearchEditor,\n\t\tSearchEditor.ID,');
    contents = `import { IModelService } from '../../../../editor/common/services/model.js';
import { ITextModelService } from '../../../../editor/common/services/resolverService.js';\n` + contents;
    contents = replaceOnce(contents, 'class SearchEditorContribution implements IWorkbenchContribution {', 'class SearchEditorContribution extends Disposable implements IWorkbenchContribution {');
    contents = replaceOnce(contents, '@IEditorResolverService editorResolverService: IEditorResolverService,', `@IModelService modelService: IModelService,
@ITextModelService textModelService: ITextModelService,
@IEditorResolverService editorResolverService: IEditorResolverService,`);
    contents = replaceOnce(contents, '\t\teditorResolverService.registerEditor(', `super();
this._register(textModelService.registerTextModelContentProvider(SearchEditorConstants.SearchEditorScheme, {
provideTextContent: async resource => modelService.getModel(resource)
}));
\t\teditorResolverService.registerEditor(`);
  }
  if (matches(searchEditorSourcePaths[5])) {
    contents = replaceOnce(contents, 'private queryEditorContainer!: HTMLElement;', 'protected queryEditorContainer!: HTMLElement;');
    contents = replaceOnce(contents, 'private pauseSearching: boolean = false;', 'protected pauseSearching: boolean = false;\nprivate searchGeneration = 0;');
    contents = replaceOnce(contents, 'private readConfigFromWidget(): SearchConfiguration {\n\t\treturn {',
      'private readConfigFromWidget(): SearchConfiguration {\n\t\treturn {\nscope: this.getInput()?.tryReadConfigSync()?.scope,');
    const build = `\t\tconst folderResources = this.contextService.getWorkspace().folders;
\t\tlet query: ITextQuery;
\t\ttry {
\t\t\tconst queryBuilder = this.instantiationService.createInstance(QueryBuilder);
\t\t\tquery = queryBuilder.text(content, folderResources.map(folder => folder.uri), options);
\t\t}
\t\tcatch (err) {
\t\t\treturn;
\t\t}`;
    contents = replaceOnce(contents, build, 'const query = this.buildTextQuery(content, options, config);');
    contents = replaceOnce(contents, 'private async doRunSearch() {\n\t\tthis.searchModel.cancelSearch(true);', `protected cancelInputSearch(): void {
++this.searchGeneration;
this.runSearchDelayer.cancel();
this.searchModel.cancelSearch(true);
}
protected buildTextQuery(content: IPatternInfo, options: ITextQueryBuilderOptions, _config: SearchConfiguration): ITextQuery {
return this.instantiationService.createInstance(QueryBuilder).text(content, this.contextService.getWorkspace().folders.map(folder => folder.uri), options);
}
private async doRunSearch() {
const generation = ++this.searchGeneration;
this.searchModel.cancelSearch(true);`);
    contents = replaceOnce(contents, 'configurationModel.updateConfig(config);',
      'if (this.input !== startInput || generation !== this.searchGeneration) return;\nconfigurationModel.updateConfig(config);');
    contents = replaceOnce(contents, '\t\tthis.searchOperation.start(500);\n\t\tthis.ongoingOperations++;\n\n', '');
    contents = replaceOnce(contents, 'const result = this.searchModel.search(query);',
      'this.searchOperation.start(500);\nthis.ongoingOperations++;\nconst result = this.searchModel.search(query);');
    contents = replaceOnce(contents, 'await this.onSearchComplete(searchOperation, config, startInput);',
      'await this.onSearchComplete(searchOperation, config, startInput, generation);\nreturn generation;');
    contents = replaceOnce(contents, 'await this.doRunSearch();',
      'const generation = await this.doRunSearch();\nif (generation === undefined || generation !== this.searchGeneration) return;');
    contents = replaceOnce(contents, 'private async onSearchComplete(searchOperation: ISearchComplete, startConfig: SearchConfiguration, startInput: SearchEditorInput) {',
      'private async onSearchComplete(searchOperation: ISearchComplete, startConfig: SearchConfiguration, startInput: SearchEditorInput, generation: number) {');
    contents = replaceOnce(contents, 'if (!input ||\n\t\t\tinput !== startInput ||', 'if (!input || generation !== this.searchGeneration ||\n\t\t\tinput !== startInput ||');
    contents = replaceOnce(contents, 'const controller = ReferencesController.get(this.searchResultEditor);',
      'if (generation !== this.searchGeneration || this.input !== startInput) return;\nconst controller = ReferencesController.get(this.searchResultEditor);');
    contents = replaceOnce(contents, 'const labelFormatter = (uri: URI): string => this.labelService.getUriLabel(uri, { relative: true });',
      'const labelFormatter = (uri: URI): string => uri.toString();');
    contents = replaceOnce(contents, '@ILabelService private readonly labelService: ILabelService,', '@ILabelService _labelService: ILabelService,');
    contents = replaceOnce(contents, 'const { resultsModel } = await input.resolveModels();\n\t\tthis.updatingModelForSearch = true;',
      'const { resultsModel } = await input.resolveModels();\nif (generation !== this.searchGeneration || this.input !== startInput) return;\n\t\tthis.updatingModelForSearch = true;');
    contents = replaceOnce(contents, 'this.priorConfig = config;', 'this.priorConfig = config;\nthis.getInput()?.updateSearchConfig(config);');
    contents = replaceOnce(contents, 'await super.setInput(newInput, options, context, token);',
      'this.cancelInputSearch();\nconst generation = this.searchGeneration;\nawait super.setInput(newInput, options, context, token);');
    contents = replaceOnce(contents, 'if (token.isCancellationRequested) {\n\t\t\treturn;',
      'if (token.isCancellationRequested || generation !== this.searchGeneration || this.input !== newInput) {\n\t\t\treturn;');
    contents = replaceOnce(contents, 'if (token.isCancellationRequested) { return; }',
      'if (token.isCancellationRequested || generation !== this.searchGeneration || this.input !== newInput) { return; }');
    contents = replaceOnce(contents, 'this.onSearchComplete(complete, existingConfig, newInput);',
      'this.onSearchComplete(complete, existingConfig, newInput, generation);');
    contents = replaceOnce(contents, 'override clearInput(): void {', 'override clearInput(): void {\nthis.cancelInputSearch();');
  }
  if (matches(searchEditorSourcePaths[6])) {
    contents = '/// <reference types="node" />\n' + contents;
    contents = replaceOnce(contents, 'function relativePathToUri(path: string, resultsUri: vscode.Uri): vscode.Uri | undefined {', `function relativePathToUri(path: string, resultsUri: vscode.Uri): vscode.Uri | undefined {
if (/^(boxteam-file|workbench|file|vscode-remote|vscode-userdata|untitled):/.test(path)) return vscode.Uri.parse(path, true);
// 旧结果没有明确 owner，只能查看，不能拼到当前虚拟工作区。
if (resultsUri.scheme === 'search-editor') return undefined;`);
  }
  return contents;
}
