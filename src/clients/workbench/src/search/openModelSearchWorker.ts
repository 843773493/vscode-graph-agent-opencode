import { bootstrapWebWorker } from '../../upstream/src/vs/base/common/worker/webWorkerBootstrap.js';
import { Range, type IRange } from '../../upstream/src/vs/editor/common/core/range.js';
import type { IPosition } from '../../upstream/src/vs/editor/common/core/position.js';
import { DefaultEndOfLine } from '../../upstream/src/vs/editor/common/model.js';
import { PieceTreeTextBufferBuilder } from '../../upstream/src/vs/editor/common/model/pieceTreeTextBuffer/pieceTreeTextBufferBuilder.js';
import type { TextModel } from '../../upstream/src/vs/editor/common/model/textModel.js';
import { SearchParams, TextModelSearch } from '../../upstream/src/vs/editor/common/model/textModelSearch.js';

/** 每次查询独占 worker；取消和期限由主线程终止，不等待正则引擎返回。 */
export class OpenModelSearchWorker {
  _requestHandlerBrand: void = undefined;

  $findMatches(text: string, pattern: string, sensitive: boolean, separators: string | null, limit: number): IRange[] {
    const builder = new PieceTreeTextBufferBuilder();
    builder.acceptChunk(text);
    const { textBuffer, disposable } = builder.finish().create(DefaultEndOfLine.LF);
    try {
      // 原匹配器只消费文本访问方法；模型身份及版本仍在主线程校验。
      const model = {
        getLineContent: (line: number) => textBuffer.getLineContent(line),
        getValueInRange: textBuffer.getValueInRange.bind(textBuffer),
        getOffsetAt: (position: IPosition) => textBuffer.getOffsetAt(position.lineNumber, position.column),
        getPositionAt: textBuffer.getPositionAt.bind(textBuffer),
        getEOL: () => textBuffer.getEOL(),
      } as TextModel;
      const lastLine = textBuffer.getLineCount();
      return TextModelSearch.findMatches(model, new SearchParams(pattern, true, sensitive, separators),
        new Range(1, 1, lastLine, textBuffer.getLineLength(lastLine) + 1), false, limit).map(match => match.range);
    } finally {
      disposable.dispose();
    }
  }
}

bootstrapWebWorker(() => new OpenModelSearchWorker());
