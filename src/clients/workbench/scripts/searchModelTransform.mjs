import { join } from 'node:path';

export const searchModelSourcePaths = [
  'src/vs/workbench/contrib/search/browser/searchTreeModel/fileMatch.ts',
  'src/vs/workbench/contrib/search/browser/notebookSearch/notebookSearchModel.ts',
  'src/vs/workbench/contrib/search/browser/AISearch/aiSearchModel.ts',
];

/** 原结果模型持有更新生命周期，正则与首次搜索共用 worker 和快照预览。 */
export function transformSearchModel(root, path, source, replaceOnce) {
  const upstream = join(root, 'upstream');
  if (!searchModelSourcePaths.some(file => path === join(upstream, file))) return source;
  let contents = `import { IWebWorkerService } from ${JSON.stringify(join(upstream, 'src/vs/platform/webWorker/browser/webWorkerService.js'))};\n` + source;
  if (path !== join(upstream, searchModelSourcePaths[0])) {
    const label = path === join(upstream, searchModelSourcePaths[1]) ? '@ILabelService labelService: ILabelService,' : '@ILabelService labelService: ILabelService,\n\t) {\n\t\tsuper({ pattern: _query }';
    contents = replaceOnce(contents, label, label.replace('@ILabelService labelService: ILabelService,', '@ILabelService labelService: ILabelService,\n@IWebWorkerService workers: IWebWorkerService,'));
    contents = replaceOnce(contents, 'modelService, replaceService, labelService);', 'modelService, replaceService, labelService, workers);');
    return path === join(upstream, searchModelSourcePaths[1])
      ? replaceOnce(contents, 'this.bindModel(model);\n\t\t\tthis.updateMatchesForModel();', 'super.createMatches();') : contents;
  }
  contents = `import { OpenModelSearch } from ${JSON.stringify(join(root, 'src/search/openModelSearch.js'))};
import { CancellationError, onUnexpectedError } from '../../../../../base/common/errors.js';
import type { IRange } from '../../../../../editor/common/core/range.js';\n` + contents;
  contents = replaceOnce(contents, 'Disposable, DisposableStore', 'Disposable, DisposableStore, MutableDisposable');
  contents = replaceOnce(contents, '@ILabelService labelService: ILabelService,', '@ILabelService labelService: ILabelService,\n@IWebWorkerService private readonly workers: IWebWorkerService,');
  contents = replaceOnce(contents, 'private _model: ITextModel | null = null;', 'private _model: ITextModel | null = null;\nprivate readonly regexSearch = this._register(new MutableDisposable<OpenModelSearch>());');
  // 首次结果已经来自搜索快照，绑定模型只负责后续变化及高亮，不重跑同一正则。
  const createStart = contents.indexOf('\tcreateMatches(): void {');
  const createEnd = contents.indexOf('\tbindModel(model:', createStart);
  if (createStart < 0 || createEnd < 0) throw new Error('原搜索结果初始化锚点不匹配');
  contents = replaceOnce(contents, contents.slice(createStart, createEnd), `	createMatches(): void {
    const model = this.modelService.getModel(this._resource);
    if (model) this.bindModel(model);
    this.rawMatch.results?.filter(resultIsMatch).forEach(result => {
      textSearchResultToMatches(result, this, false).forEach(match => this.add(match));
    });
    this.addContext(this.rawMatch.results);
    this.updateHighlights();
  }
`);
  contents = replaceOnce(contents, 'this._model = model;', 'this.regexSearch.clear();\nthis._model = model;');
  contents = replaceOnce(contents, 'this._updateScheduler.schedule();', 'this.regexSearch.clear();\nthis._updateScheduler.schedule();');
  const updateStart = contents.indexOf('\tprotected updateMatchesForModel(): void {');
  const updateEnd = contents.indexOf('\tprivate updateMatches(', updateStart);
  if (updateStart < 0 || updateEnd < 0) throw new Error('原搜索结果更新锚点不匹配');
  contents = replaceOnce(contents, contents.slice(updateStart, updateEnd), `  private async updateRegexMatches(range: IRange | undefined, accept: (results: ITextSearchResult[]) => void): Promise<void> {
    const model = this._model!;
    const search = new OpenModelSearch(this.workers, undefined, Date.now() + 10000);
    this.regexSearch.value = search;
    try {
      const result = await search.findMatches(model.getValue(), { pattern: this._query,
        maxResults: this._maxResults ?? DEFAULT_MAX_SEARCH_RESULTS, previewOptions: this._previewOptions,
        surroundingContext: this.parent().parent().query?.surroundingContext, range });
      if (this.regexSearch.value !== search) throw new CancellationError();
      accept(result.results);
    } finally {
      if (this.regexSearch.value === search) this.regexSearch.clear();
    }
  }

  private textResults(matches: FindMatch[], model: ITextModel): ITextSearchResult[] {
    return getTextSearchMatchWithModelContext(editorMatchesToTextSearchResults(matches, model, this._previewOptions),
      model, this.parent().parent().query!);
  }

  protected updateMatchesForModel(): void {
    if (!this._model) return;
    if (this._query.isRegExp) {
      void this.updateRegexMatches(undefined, results => {
        this._textMatches = new Map();
        this.updateMatches(results, true, false);
      }).catch(onUnexpectedError);
      return;
    }
    const wordSeparators = this._query.isWordMatch && this._query.wordSeparators ? this._query.wordSeparators : null;
    const matches = this._model.findMatches(this._query.pattern, this._model.getFullModelRange(), false,
      !!this._query.isCaseSensitive, wordSeparators, false, this._maxResults ?? DEFAULT_MAX_SEARCH_RESULTS);
    this._textMatches = new Map();
    this.updateMatches(this.textResults(matches, this._model), true, false);
  }

  protected async updatesMatchesForLineAfterReplace(lineNumber: number, modelChange: boolean): Promise<void> {
    if (!this._model) return;
    this._updateScheduler.cancel();
    const range = { startLineNumber: lineNumber, startColumn: this._model.getLineMinColumn(lineNumber),
      endLineNumber: lineNumber, endColumn: this._model.getLineMaxColumn(lineNumber) };
    const wordSeparators = this._query.isWordMatch && this._query.wordSeparators ? this._query.wordSeparators : null;
    const accept = (results: ITextSearchResult[]) => {
      this._textMatches.forEach(match => {
        if (match.range().startLineNumber === lineNumber) this._textMatches.delete(match.id());
      });
      this.updateMatches(results, modelChange, false);
    };
    if (this._query.isRegExp) await this.updateRegexMatches(range, accept);
    else accept(this.textResults(this._model.findMatches(this._query.pattern, range, false,
      !!this._query.isCaseSensitive, wordSeparators, false, this._maxResults ?? DEFAULT_MAX_SEARCH_RESULTS), this._model));
  }

`);
  contents = replaceOnce(contents, 'private updateMatches(matches: FindMatch[], modelChange: boolean, model: ITextModel, isAiContributed: boolean): void {\n\t\tconst textSearchResults = editorMatchesToTextSearchResults(matches, model, this._previewOptions);',
    'private updateMatches(textSearchResults: ITextSearchResult[], modelChange: boolean, isAiContributed: boolean): void {');
  contents = replaceOnce(contents, 'textSearchResults.forEach(textSearchResult => {', 'textSearchResults.filter(resultIsMatch).forEach(textSearchResult => {');
  contents = replaceOnce(contents, 'this.addContext(getTextSearchMatchWithModelContext(textSearchResults, model, this.parent().parent().query!));', 'this.addContext(textSearchResults);');
  return contents;
}
