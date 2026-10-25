export interface VisibleSourceLine {
  lineNumber: number;
  line: string;
}

/** 给源码预览建立换行偏移索引，避免为整份文件拆出行字符串。 */
export function createSourceLineStarts(source: string): number[] {
  const lineStartOffsets = [0];
  for (
    let newlineOffset = source.indexOf("\n");
    newlineOffset !== -1;
    newlineOffset = source.indexOf("\n", newlineOffset + 1)
  ) {
    lineStartOffsets.push(newlineOffset + 1);
  }
  return lineStartOffsets;
}

/** 只切出源码视窗需要的行，同时保持原有行号边界规则。 */
export function visibleSourceLines(
  source: string,
  lineStartOffsets: readonly number[],
  focusLine: number | null,
  radius: number,
): VisibleSourceLine[] {
  const lineCount = lineStartOffsets.length;
  if (lineCount === 0) return [];

  const center = Math.max(1, Math.min(focusLine ?? 1, lineCount));
  const visibleRangeStart = Math.max(0, center - 1 - radius);
  const firstLineIndex = Math.trunc(visibleRangeStart);
  const endLineIndex = Math.trunc(Math.min(lineCount, center + radius));
  const result: VisibleSourceLine[] = [];

  for (let lineIndex = firstLineIndex; lineIndex < endLineIndex; lineIndex += 1) {
    const startOffset = lineStartOffsets[lineIndex]!;
    const nextLineStart = lineStartOffsets[lineIndex + 1];
    const endOffset = nextLineStart === undefined ? source.length : nextLineStart - 1;
    result.push({
      lineNumber: visibleRangeStart + lineIndex - firstLineIndex + 1,
      line: source.slice(startOffset, endOffset),
    });
  }
  return result;
}
