import { join } from 'node:path';

export const findSourcePaths = [
  'src/vs/editor/contrib/find/browser/findModel.ts',
  'src/vs/editor/contrib/find/browser/findController.ts',
  'src/vs/editor/contrib/find/browser/findWidget.ts',
  'src/vs/editor/browser/editorExtensions.ts',
  'src/vs/editor/contrib/multicursor/browser/multicursor.ts',
];

/** 沿原查找模型异步结算；查找、导航、捕获组替换不再同步执行正则。 */
export function transformFind(root, path, source, replaceOnce) {
  const upstream = join(root, 'upstream'), index = findSourcePaths.findIndex(file => path === join(upstream, file));
  if (index < 0) return source;
  let contents = source;
  const every = (before, after, count) => {
    if (contents.split(before).length - 1 !== count) throw new Error(`原查找锚点数量不匹配：${before}`);
    contents = contents.replaceAll(before, after);
  };
  if (index === 0) {
    contents = `import { OpenModelSearch } from ${JSON.stringify(join(root, 'src/search/openModelSearch.js'))};
import { CancellationError, onUnexpectedError } from '../../../../base/common/errors.js';
import { CancellationTokenSource } from '../../../../base/common/cancellation.js';
import { IWebWorkerService } from '../../../../platform/webWorker/browser/webWorkerService.js';
import type { IPatternInfo } from '../../../../workbench/services/search/common/search.js';\n` + contents;
    contents = replaceOnce(contents, 'RunOnceScheduler, TimeoutTimer', 'RunOnceScheduler, TimeoutTimer, Sequencer');
    contents = replaceOnce(contents, "import { SearchParams } from '../../../common/model/textModelSearch.js';\n", '');
    contents = replaceOnce(contents, 'constructor(editor: IActiveCodeEditor, state: FindReplaceState)', 'constructor(editor: IActiveCodeEditor, state: FindReplaceState, private readonly workers: IWebWorkerService)');
    contents = replaceOnce(contents, 'private _isDisposed: boolean;', `private _isDisposed: boolean;
private readonly operations = new Sequencer();
private generation = 0;
private activeSearch: CancellationTokenSource | undefined;

private invalidateSearch(): void {
  ++this.generation;
  this.activeSearch?.cancel();
}

private runOperation<T>(run: () => Promise<T>): Promise<T> {
  const generation = this.generation;
  return this.operations.queue(async () => {
    if (this._isDisposed || generation !== this.generation) throw new CancellationError();
    const source = this.activeSearch = new CancellationTokenSource();
    this._state.change({ isSearching: true }, false);
    try { return await run(); }
    finally {
      source.dispose(true);
      this.activeSearch = undefined;
      if (!this._isDisposed && generation === this.generation) this._state.change({ isSearching: false }, false);
    }
  });
}

private pattern(): IPatternInfo {
  return { pattern: this._state.searchString, isRegExp: this._state.isRegex, isCaseSensitive: this._state.matchCase,
    isWordMatch: this._state.wholeWord, wordSeparators: this._editor.getOption(EditorOption.wordSeparators) };
}

private async withSearch<T>(run: (search: OpenModelSearch, text: string, pattern: IPatternInfo) => Promise<T>): Promise<T> {
  const search = new OpenModelSearch(this.workers, this.activeSearch!.token);
  try { return await run(search, this._editor.getModel().getValue(EndOfLinePreference.LF), this.pattern()); }
  finally { search.dispose(); }
}

private async findSingle(position: Position, previous: boolean, captureMatches: boolean): Promise<FindMatch | null> {
  if (this._state.isRegex) return this.withSearch((search, text, pattern) => search.findEditorMatch(text, pattern,
    this._editor.getModel().validatePosition(position), previous, captureMatches));
  const model = this._editor.getModel(), separators = this._state.wholeWord ? this._editor.getOption(EditorOption.wordSeparators) : null;
  return previous ? model.findPreviousMatch(this._state.searchString, position, false, this._state.matchCase, separators, captureMatches)
    : model.findNextMatch(this._state.searchString, position, false, this._state.matchCase, separators, captureMatches);
}`);
    contents = replaceOnce(contents, 'this._isDisposed = true;', 'this._isDisposed = true;\nthis.invalidateSearch();\nthis._state.change({ isSearching: false }, false);');
    contents = replaceOnce(contents, 'if (e.isFlush) {', 'this.invalidateSearch();\nif (e.isFlush) {');
    contents = replaceOnce(contents, 'this._decorations.setStartPosition(this._editor.getPosition());\n\t\t\t}', `this._decorations.setStartPosition(this._editor.getPosition());
if (e.reason === CursorChangeReason.Explicit && this.activeSearch) {
  this.invalidateSearch();
  this._updateDecorationsScheduler.schedule();
}
\t\t\t}`);
    contents = replaceOnce(contents, 'const model = this._editor.getModel();\n\n\t\t\tif (model.isTooLargeForSyncing())', 'this.invalidateSearch();\nconst model = this._editor.getModel();\n\n\t\t\tif (model.isTooLargeForSyncing())');
    contents = replaceOnce(contents, 'private research(moveCursor: boolean, newFindScope?: Range | Range[] | null): void {', `private research(moveCursor: boolean, newFindScope?: Range | Range[] | null): void {
  void this.runOperation(() => this.doResearch(moveCursor, newFindScope)).catch(onUnexpectedError);
}

private async doResearch(moveCursor: boolean, newFindScope?: Range | Range[] | null): Promise<void> {`);
    contents = replaceOnce(contents, 'const findMatches = this._findMatches(findScopes, false, MATCHES_LIMIT);', 'const findMatches = await this._findMatches(findScopes, false, MATCHES_LIMIT);');
    contents = replaceOnce(contents, 'this._moveToNextMatch(this._decorations.getStartPosition());', 'await this._moveToNextMatch(this._decorations.getStartPosition());');
    contents = replaceOnce(contents, 'private _moveToPrevMatch(before: Position, isRecursed: boolean = false): void', 'private async _moveToPrevMatch(before: Position, isRecursed: boolean = false): Promise<void>');
    contents = replaceOnce(contents, 'private _moveToNextMatch(after: Position): void', 'private async _moveToNextMatch(after: Position): Promise<void>');
    contents = replaceOnce(contents, 'private _getNextMatch(after: Position, captureMatches: boolean, forceMove: boolean, isRecursed: boolean = false): FindMatch | null', 'private async _getNextMatch(after: Position, captureMatches: boolean, forceMove: boolean, isRecursed: boolean = false): Promise<FindMatch | null>');
    const previous = 'model.findPreviousMatch(this._state.searchString, position, this._state.isRegex, this._state.matchCase, this._state.wholeWord ? this._editor.getOption(EditorOption.wordSeparators) : null, false)';
    const next = 'model.findNextMatch(this._state.searchString, position, this._state.isRegex, this._state.matchCase, this._state.wholeWord ? this._editor.getOption(EditorOption.wordSeparators) : null, captureMatches)';
    every(previous, 'await this.findSingle(position, true, false)', 2);
    every(next, 'await this.findSingle(position, false, captureMatches)', 2);
    for (const position of ['before', 'after']) {
      contents = replaceOnce(contents, `const { lineNumber, column } = ${position};\n\t\tconst model = this._editor.getModel();`, `const { lineNumber, column } = ${position};`);
    }
    contents = replaceOnce(contents, 'const nextMatch = this._getNextMatch(after, false, true);', 'const nextMatch = await this._getNextMatch(after, false, true);');
    contents = replaceOnce(contents, 'public moveToPrevMatch(): void {\n\t\tthis._moveToPrevMatch(this._editor.getSelection().getStartPosition());', 'public moveToPrevMatch(): Promise<void> {\nreturn this.runOperation(() => this._moveToPrevMatch(this._editor.getSelection().getStartPosition()));');
    contents = replaceOnce(contents, 'public moveToNextMatch(): void {\n\t\tthis._moveToNextMatch(this._editor.getSelection().getEndPosition());', 'public moveToNextMatch(): Promise<void> {\nreturn this.runOperation(() => this._moveToNextMatch(this._editor.getSelection().getEndPosition()));');
    for (const method of ['replace', 'replaceAll', 'selectAllMatches']) {
      const replacing = method !== 'selectAllMatches';
      contents = replaceOnce(contents, `public ${method}(): void {`, `public ${method}(): Promise<void> {
${replacing ? 'const replacement = this._state.replaceString, preserveCase = this._state.preserveCase;' : ''}
return this.runOperation(() => this.do${method}(${replacing ? 'replacement, preserveCase' : ''}));
}
private async do${method}(${replacing ? 'replacement: string, preserveCase: boolean' : ''}): Promise<void> {`);
    }
    contents = replaceOnce(contents, 'private _getReplacePattern(): ReplacePattern', 'private _getReplacePattern(replacement: string): ReplacePattern');
    contents = replaceOnce(contents, 'return parseReplaceString(this._state.replaceString);', 'return parseReplaceString(replacement);');
    contents = replaceOnce(contents, 'return ReplacePattern.fromStaticValue(this._state.replaceString);', 'return ReplacePattern.fromStaticValue(replacement);');
    every('const replacePattern = this._getReplacePattern();', 'const replacePattern = this._getReplacePattern(replacement);', 3);
    contents = replaceOnce(contents, 'buildReplaceString(nextMatch.matches, this._state.preserveCase)', 'buildReplaceString(nextMatch.matches, preserveCase)');
    contents = replaceOnce(contents, 'const nextMatch = this._getNextMatch(selection.getStartPosition(), true, false);', 'const nextMatch = await this._getNextMatch(selection.getStartPosition(), true, false);');
    contents = replaceOnce(contents, 'this.research(true);', 'await this.doResearch(true);');
    contents = replaceOnce(contents, 'private _findMatches(findScopes: Range[] | null, captureMatches: boolean, limitResultCount: number): FindMatch[]', 'private async _findMatches(findScopes: Range[] | null, captureMatches: boolean, limitResultCount: number): Promise<FindMatch[]>');
    contents = replaceOnce(contents, 'private async _findMatches(', `public findAllMatches(findScopes: Range[] | null): Promise<FindMatch[]> {
  return this.runOperation(() => this._findMatches(findScopes, false, Constants.MAX_SAFE_SMALL_INTEGER));
}

private async _findMatches(`);
    contents = replaceOnce(contents, 'return this._editor.getModel().findMatches(this._state.searchString, searchRanges,', `if (this._state.isRegex) return this.withSearch((search, text, pattern) => search.findEditorMatches(text, pattern,
  searchRanges.map(range => this._editor.getModel().validateRange(range)), captureMatches, limitResultCount));
return this._editor.getModel().findMatches(this._state.searchString, searchRanges,`);
    contents = replaceOnce(contents, 'this._largeReplaceAll();', 'await this._largeReplaceAll(replacement, preserveCase);');
    contents = replaceOnce(contents, 'this._regularReplaceAll(findScopes);', 'await this._regularReplaceAll(findScopes, replacement, preserveCase);');
    contents = replaceOnce(contents, '\t\tthis.research(false);\n\t}\n\n\tprivate _largeReplaceAll()', '\t\tawait this.doResearch(false);\n\t}\n\n\tprivate _largeReplaceAll()');
    const largeStart = contents.indexOf('\tprivate _largeReplaceAll(): void {'), largeEnd = contents.indexOf('\tprivate _regularReplaceAll(', largeStart);
    if (largeStart < 0 || largeEnd < 0) throw new Error('原大量替换锚点不匹配');
    contents = replaceOnce(contents, contents.slice(largeStart, largeEnd), `private async _largeReplaceAll(replacement: string, preserveCase: boolean): Promise<void> {
  const model = this._editor.getModel(), selection = this._editor.getSelection(), range = model.getFullModelRange();
  const result = await this.withSearch((search, text, pattern) => search.replaceAll(text,
    pattern, replacement, preserveCase));
  this._executeEditorCommand('replaceAll', new ReplaceCommandThatPreservesSelection(range, result, selection));
}

`);
    contents = replaceOnce(contents, 'private _regularReplaceAll(findScopes: Range[] | null): void', 'private async _regularReplaceAll(findScopes: Range[] | null, replacement: string, preserveCase: boolean): Promise<void>');
    contents = replaceOnce(contents, 'const matches = this._findMatches(findScopes, replacePattern.hasReplacementPatterns || this._state.preserveCase, Constants.MAX_SAFE_SMALL_INTEGER);', 'const matches = await this._findMatches(findScopes, replacePattern.hasReplacementPatterns || preserveCase, Constants.MAX_SAFE_SMALL_INTEGER);');
    contents = replaceOnce(contents, 'buildReplaceString(matches[i].matches, this._state.preserveCase)', 'buildReplaceString(matches[i].matches, preserveCase)');
    contents = replaceOnce(contents, 'const matches = this._findMatches(findScopes, false, Constants.MAX_SAFE_SMALL_INTEGER);', 'const matches = await this._findMatches(findScopes, false, Constants.MAX_SAFE_SMALL_INTEGER);');
  }
  if (index === 1) {
    contents = "import { IWebWorkerService } from '../../../../platform/webWorker/browser/webWorkerService.js';\n" + contents;
    contents = replaceOnce(contents, '@IHoverService hoverService: IHoverService\n', '@IHoverService hoverService: IHoverService,\n@IWebWorkerService private readonly workers: IWebWorkerService\n');
    contents = replaceOnce(contents, '@IAccessibilityService private readonly _accessibilityService: IAccessibilityService,', '@IAccessibilityService private readonly _accessibilityService: IAccessibilityService,\n@IWebWorkerService workers: IWebWorkerService,');
    contents = replaceOnce(contents, 'super(editor, _contextKeyService, _storageService, clipboardService, notificationService, hoverService);', 'super(editor, _contextKeyService, _storageService, clipboardService, notificationService, hoverService, workers);');
    contents = replaceOnce(contents, 'new FindModelBoundToEditorModel(this._editor, this._state)', 'new FindModelBoundToEditorModel(this._editor, this._state, this.workers)');
    contents = replaceOnce(contents, 'public selectAllMatches(): boolean', `public async findAllMatches(findScopes: Range[] | null): Promise<FindMatch[]> {
  if (!this._model) throw new Error('查找模型尚未初始化');
  return this._model.findAllMatches(findScopes);
}

public selectAllMatches(): boolean`);
    contents = "import type { Range } from '../../../common/core/range.js';\n" + contents;
    contents = replaceOnce(contents, 'import { OverviewRulerLane }', 'import { OverviewRulerLane, type FindMatch }');
    for (const method of ['moveToNextMatch', 'moveToPrevMatch', 'replace', 'replaceAll', 'selectAllMatches']) {
      contents = replaceOnce(contents, `public ${method}(): boolean`, `public async ${method}(): Promise<boolean>`);
      contents = replaceOnce(contents, `this._model.${method}();`, `await this._model.${method}();`);
    }
    every('protected abstract _run(controller: CommonFindController): boolean;', 'protected abstract _run(controller: CommonFindController): Promise<boolean>;', 2);
    every('protected _run(controller: CommonFindController): boolean {', 'protected _run(controller: CommonFindController): Promise<boolean> {', 2);
    contents = replaceOnce(contents, 'controller && !this._run(controller)', 'controller && !await this._run(controller)');
    contents = replaceOnce(contents, 'if (!this._run(controller))', 'if (!await this._run(controller))');
    every('this._run(controller);', 'await this._run(controller);', 2);
    contents = replaceOnce(contents, 'const runMatch = (): boolean => {', 'const runMatch = async (): Promise<boolean> => {');
    contents = replaceOnce(contents, 'const result = next ? controller.moveToNextMatch() : controller.moveToPrevMatch();', 'const result = await (next ? controller.moveToNextMatch() : controller.moveToPrevMatch());');
    every('if (!runMatch())', 'if (!await runMatch())', 2);
  }
  if (index === 2) {
    contents = replaceOnce(contents, 'replace(): void;', 'replace(): Promise<boolean>;');
    contents = replaceOnce(contents, 'replaceAll(): void;', 'replaceAll(): Promise<boolean>;');
    every('if (e.searchString || e.matchesCount || e.matchesPosition)', 'if (e.searchString || e.matchesCount || e.matchesPosition || e.isSearching)', 1);
    contents = replaceOnce(contents, '(this._state.searchString.length > 0 && this._state.matchesCount === 0)', '(!this._state.isSearching && this._state.searchString.length > 0 && this._state.matchesCount === 0)');
    contents = replaceOnce(contents, 'if (this._state.matchesCount > 0) {\n\t\t\tlet matchesCount:', 'if (this._state.isSearching) {\nlabel = nls.localize("findSearching", "Searching…");\n} else if (this._state.matchesCount > 0) {\n\t\t\tlet matchesCount:');
    contents = replaceOnce(contents, 'const findInputIsNonEmpty = (this._state.searchString.length > 0);', 'const findInputIsNonEmpty = (!this._state.isSearching && this._state.searchString.length > 0);');
    contents = replaceOnce(contents, 'this._controller.replace();', 'void this._controller.replace().catch(onUnexpectedError);');
    contents = replaceOnce(contents, 'this._controller.replaceAll();', 'void this._controller.replaceAll().catch(onUnexpectedError);');
  }
  if (index === 3) {
    // 原贡献命令丢弃回调返回值；传回实际完成，键盘和命令调用才能等待异步替换。
    every('(controller: T, args: unknown) => void;', '(controller: T, args: unknown) => unknown;', 2);
    contents = replaceOnce(contents, 'public runEditorCommand(accessor: ServicesAccessor, editor: ICodeEditor, args: unknown): void {',
      'public async runEditorCommand(accessor: ServicesAccessor, editor: ICodeEditor, args: unknown): Promise<void> {');
    contents = replaceOnce(contents, 'this._callback(controller, args);', 'await this._callback(controller, args);');
  }
  if (index === 4) {
    contents = replaceOnce(contents, 'const cursorDiff = cursorState.filter(cs => !previousCursorState.find(pcs => pcs.equals(cs)));', `const positionKey = (state: CursorState) => state.modelState.position.lineNumber + ':' + state.modelState.position.column;
const previousByPosition = new Map<string, CursorState[]>();
for (const state of previousCursorState) {
  const key = positionKey(state), states = previousByPosition.get(key);
  if (states) states.push(state);
  else previousByPosition.set(key, [state]);
}
const cursorDiff = cursorState.filter(state => !previousByPosition.get(positionKey(state))?.some(previous => previous.equals(state)));`);
    contents = replaceOnce(contents, 'public selectAll(findController: CommonFindController): void', 'public async selectAll(findController: CommonFindController): Promise<void>');
    const start = contents.indexOf('\t\t\tconst editorModel = this._editor.getModel();', contents.indexOf('const allowFindRegex ='));
    const end = contents.indexOf('\n\t\t} else {', start);
    if (start < 0 || end < 0) throw new Error('原多光标正则查找锚点不匹配');
    contents = replaceOnce(contents, contents.slice(start, end), '\t\t\tmatches = await findController.findAllMatches(findState.searchScope);');
    const actionStart = contents.indexOf('export abstract class MultiCursorSelectionControllerAction');
    const actionEnd = contents.indexOf('export class AddSelectionToNextFindMatchAction', actionStart);
    const action = contents.slice(actionStart, actionEnd);
    contents = replaceOnce(contents, action, action
      .replace('public run(accessor: ServicesAccessor, editor: ICodeEditor): void', 'public async run(accessor: ServicesAccessor, editor: ICodeEditor): Promise<void>')
      .replace('this._run(multiCursorController, findController);', 'await this._run(multiCursorController, findController);')
      .replace('this._run(multiCursorController, newFindController);\n\t\t\t\tnewFindController.dispose();', 'try { await this._run(multiCursorController, newFindController); }\nfinally { newFindController.dispose(); }')
      .replace('findController: CommonFindController): void;', 'findController: CommonFindController): void | Promise<void>;'));
    every('protected _run(multiCursorController: MultiCursorSelectionController, findController: CommonFindController): void {\n\t\tmultiCursorController.selectAll(findController);',
      'protected _run(multiCursorController: MultiCursorSelectionController, findController: CommonFindController): Promise<void> {\nreturn multiCursorController.selectAll(findController);', 2);
  }
  return contents;
}
