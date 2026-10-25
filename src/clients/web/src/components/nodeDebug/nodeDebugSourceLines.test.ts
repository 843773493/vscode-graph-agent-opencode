import { describe, expect, test } from "bun:test";
import { createSourceLineStarts, visibleSourceLines } from "./nodeDebugSourceLines";

describe("Node 调试源码行索引", () => {
  test("保留空文件与末尾换行对应的空行", () => {
    expect(visibleSourceLines("", createSourceLineStarts(""), null, 7)).toEqual([
      { lineNumber: 1, line: "" },
    ]);
    expect(visibleSourceLines("first\n", createSourceLineStarts("first\n"), 2, 7)).toEqual([
      { lineNumber: 1, line: "first" },
      { lineNumber: 2, line: "" },
    ]);
  });

  test("保留 CRLF 行内容并只返回焦点附近的窗口", () => {
    const source = Array.from({ length: 30 }, (_, index) => `line-${index + 1}`).join("\r\n");
    const lineStarts = createSourceLineStarts(source);

    const lines = visibleSourceLines(source, lineStarts, 15, 7);

    expect(lines).toHaveLength(15);
    expect(lines[0]).toEqual({ lineNumber: 8, line: "line-8\r" });
    expect(lines[lines.length - 1]).toEqual({ lineNumber: 22, line: "line-22\r" });
  });

  test("焦点越界时按现有规则裁剪到文件首尾", () => {
    const source = Array.from({ length: 10 }, (_, index) => `line-${index + 1}`).join("\n");
    const lineStarts = createSourceLineStarts(source);

    expect(visibleSourceLines(source, lineStarts, 1, 7).map(({ lineNumber }) => lineNumber))
      .toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(visibleSourceLines(source, lineStarts, 99, 7).map(({ lineNumber }) => lineNumber))
      .toEqual([3, 4, 5, 6, 7, 8, 9, 10]);
  });

  test("各文件长度和焦点位置都与原 split/slice 投影一致", () => {
    for (let lineCount = 1; lineCount <= 25; lineCount += 1) {
      const content = Array.from({ length: lineCount }, (_, index) => `line-${index + 1}`).join("\n")
        + (lineCount % 2 === 0 ? "\n" : "");
      const originalLines = content.split("\n");
      const indexedLines = createSourceLineStarts(content);
      for (const focusLine of [null, -2, 0, 1, 2, 7, 15, 1.5, originalLines.length, originalLines.length + 10]) {
        const center = Math.max(1, Math.min(focusLine ?? 1, originalLines.length));
        const start = Math.max(0, center - 1 - 7);
        const end = Math.min(originalLines.length, center + 7);
        const expected = originalLines.slice(start, end).map((line, index) => ({
          lineNumber: start + index + 1,
          line,
        }));
        expect(visibleSourceLines(content, indexedLines, focusLine, 7)).toEqual(expected);
      }
    }
  });
});
