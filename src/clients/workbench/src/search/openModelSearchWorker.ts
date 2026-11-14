import { bootstrapWebWorker } from '../../upstream/src/vs/base/common/worker/webWorkerBootstrap.js';
import { Range, type IRange } from '../../upstream/src/vs/editor/common/core/range.js';
import type { IPosition } from '../../upstream/src/vs/editor/common/core/position.js';
import { DefaultEndOfLine } from '../../upstream/src/vs/editor/common/model.js';
import { PieceTreeTextBufferBuilder } from '../../upstream/src/vs/editor/common/model/pieceTreeTextBuffer/pieceTreeTextBufferBuilder.js';
import type { TextModel } from '../../upstream/src/vs/editor/common/model/textModel.js';
import { SearchParams, TextModelSearch } from '../../upstream/src/vs/editor/common/model/textModelSearch.js';
import type { IPatternInfo, ITextSearchPreviewOptions, ITextSearchQuery } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { editorMatchesToTextSearchResults, getTextSearchMatchWithModelContext } from '../../upstream/src/vs/workbench/services/search/common/searchHelpers.js';

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

  $findMatches(text: string, request: OpenModelSearchRequest) {
    const builder = new PieceTreeTextBufferBuilder();
    builder.acceptChunk(text);
    const { textBuffer, disposable } = builder.finish().create(DefaultEndOfLine.LF);
    try {
      const model = {
        getLineContent: (line: number) => textBuffer.getLineContent(line),
        getLineCount: () => textBuffer.getLineCount(),
        getValueInRange: textBuffer.getValueInRange.bind(textBuffer),
        getOffsetAt: (position: IPosition) => textBuffer.getOffsetAt(position.lineNumber, position.column),
        getPositionAt: textBuffer.getPositionAt.bind(textBuffer),
        getEOL: () => textBuffer.getEOL(),
      } as TextModel;
      const { pattern, maxResults, range } = request, lastLine = textBuffer.getLineCount();
      const matches = TextModelSearch.findMatches(model,
        new SearchParams(pattern.pattern, true, !!pattern.isCaseSensitive, pattern.isWordMatch ? pattern.wordSeparators ?? null : null),
        range ? Range.lift(range) : new Range(1, 1, lastLine, textBuffer.getLineLength(lastLine) + 1), false, maxResults + 1);
      return { count: matches.length, results: getTextSearchMatchWithModelContext(
        editorMatchesToTextSearchResults(matches.slice(0, maxResults), model, request.previewOptions), model,
        { surroundingContext: request.surroundingContext } as ITextSearchQuery) };
    } finally {
      disposable.dispose();
    }
  }
}

bootstrapWebWorker(() => new OpenModelSearchWorker());
