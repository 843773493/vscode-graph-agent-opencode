import { bootstrapWebWorker } from '../../upstream/src/vs/base/common/worker/webWorkerBootstrap.js';
import { Range, type IRange } from '../../upstream/src/vs/editor/common/core/range.js';
import { Position, type IPosition } from '../../upstream/src/vs/editor/common/core/position.js';
import { DefaultEndOfLine } from '../../upstream/src/vs/editor/common/model.js';
import { PieceTreeTextBufferBuilder } from '../../upstream/src/vs/editor/common/model/pieceTreeTextBuffer/pieceTreeTextBufferBuilder.js';
import type { TextModel } from '../../upstream/src/vs/editor/common/model/textModel.js';
import { SearchParams, TextModelSearch } from '../../upstream/src/vs/editor/common/model/textModelSearch.js';
import type { IPatternInfo, ITextSearchPreviewOptions, ITextSearchQuery } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { editorMatchesToTextSearchResults, getTextSearchMatchWithModelContext } from '../../upstream/src/vs/workbench/services/search/common/searchHelpers.js';
import { parseReplaceString, ReplacePattern } from '../../upstream/src/vs/editor/contrib/find/browser/replacePattern.js';

export interface OpenModelSearchRequest {
  pattern: IPatternInfo;
  maxResults: number;
  previewOptions?: ITextSearchPreviewOptions;
  surroundingContext?: number;
  range?: IRange;
}

/** 正文和预览属于同一次快照，模型关闭后也无需再次读取。 */
export class OpenModelSearchWorker {
  _requestHandlerBrand: void = undefined;

  private withModel<T>(text: string, run: (model: TextModel) => T): T {
    const builder = new PieceTreeTextBufferBuilder();
    builder.acceptChunk(text);
    const { textBuffer, disposable } = builder.finish().create(DefaultEndOfLine.LF);
    try {
      const model = {
        getLineContent: (line: number) => textBuffer.getLineContent(line),
        getLineCount: () => textBuffer.getLineCount(),
        getLineMaxColumn: (line: number) => textBuffer.getLineLength(line) + 1,
        getValueInRange: textBuffer.getValueInRange.bind(textBuffer),
        getOffsetAt: (position: IPosition) => textBuffer.getOffsetAt(position.lineNumber, position.column),
        getPositionAt: textBuffer.getPositionAt.bind(textBuffer),
        getEOL: () => textBuffer.getEOL(),
      } as TextModel;
      return run(model);
    } finally {
      disposable.dispose();
    }
  }

  $findMatches(text: string, request: OpenModelSearchRequest) {
    return this.withModel(text, model => {
      const { pattern, maxResults, range } = request, lastLine = model.getLineCount();
      const matches = TextModelSearch.findMatches(model,
        new SearchParams(pattern.pattern, true, !!pattern.isCaseSensitive, pattern.isWordMatch ? pattern.wordSeparators ?? null : null),
        range ? Range.lift(range) : new Range(1, 1, lastLine, model.getLineMaxColumn(lastLine)), false, maxResults + 1);
      return { count: matches.length, results: getTextSearchMatchWithModelContext(
        editorMatchesToTextSearchResults(matches.slice(0, maxResults), model, request.previewOptions), model,
        { surroundingContext: request.surroundingContext } as ITextSearchQuery) };
    });
  }

  $findEditorMatches(text: string, pattern: IPatternInfo, ranges: IRange[], captureMatches: boolean, limit: number) {
    return this.withModel(text, model => {
      // 与原 TextModel.findMatches 相同：合并相交范围，每个独立范围保留原配额。
      const sorted = ranges.map(range => Range.lift(range)).sort(Range.compareRangesUsingStarts), merged: Range[] = [];
      for (const range of sorted) {
        const previous = merged[merged.length - 1];
        if (previous && Range.areIntersecting(previous, range)) merged[merged.length - 1] = previous.plusRange(range);
        else merged.push(range);
      }
      return merged.flatMap(range => TextModelSearch.findMatches(model, this.searchParams(pattern), range, captureMatches, limit));
    });
  }

  $findEditorMatch(text: string, pattern: IPatternInfo, position: IPosition, previous: boolean, captureMatches: boolean) {
    return this.withModel(text, model => previous
      ? TextModelSearch.findPreviousMatch(model, this.searchParams(pattern), Position.lift(position), captureMatches)
      : TextModelSearch.findNextMatch(model, this.searchParams(pattern), Position.lift(position), captureMatches));
  }

  $replaceAll(text: string, pattern: IPatternInfo, replacement: string, preserveCase: boolean) {
    const searchData = this.searchParams(pattern).parseSearchRequest();
    if (!searchData) return text;
    let regex = searchData.regex;
    if (!regex.multiline) regex = new RegExp(regex.source, 'mu' + (regex.ignoreCase ? 'i' : '') + (regex.global ? 'g' : ''));
    const replace = pattern.isRegExp ? parseReplaceString(replacement) : ReplacePattern.fromStaticValue(replacement);
    return replace.hasReplacementPatterns || preserveCase
      ? text.replace(regex, (...args: unknown[]) => replace.buildReplaceString(args as string[], preserveCase))
      : text.replace(regex, replace.buildReplaceString(null, preserveCase));
  }

  private searchParams(pattern: IPatternInfo): SearchParams {
    return new SearchParams(pattern.pattern, !!pattern.isRegExp, !!pattern.isCaseSensitive,
      pattern.isWordMatch ? pattern.wordSeparators ?? null : null);
  }
}

bootstrapWebWorker(() => new OpenModelSearchWorker());
