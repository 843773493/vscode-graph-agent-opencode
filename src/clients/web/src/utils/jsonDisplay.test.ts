import { describe, expect, test } from "bun:test";

import {
  LARGE_STRING_DISPLAY_LIMIT,
  boundedDisplayString,
  prettyJson,
  redactLargeData,
} from "./jsonDisplay";

describe("超大展示文本的兜底截断", () => {
  test("超长普通字符串在展示原语里截断并显式标注原文长度", () => {
    const huge = "a".repeat(LARGE_STRING_DISPLAY_LIMIT + 5000);
    const output = prettyJson({ result: huge });

    expect(output.length).toBeLessThan(LARGE_STRING_DISPLAY_LIMIT + 1000);
    expect(output).toContain("已截断展示");
    expect(output).toContain(String(huge.length));
  });

  test("boundedDisplayString 只保留头部且不改动未超限文本", () => {
    expect(boundedDisplayString("短文本")).toBe("短文本");
    const huge = "b".repeat(LARGE_STRING_DISPLAY_LIMIT * 3);
    const bounded = boundedDisplayString(huge);
    expect(bounded.startsWith("b".repeat(LARGE_STRING_DISPLAY_LIMIT))).toBe(true);
    expect(bounded.length).toBeLessThan(LARGE_STRING_DISPLAY_LIMIT + 200);
  });

  test("data URL 脱敏与普通字段脱敏行为保持不变", () => {
    const redacted = redactLargeData({
      blob: "data:image/png;base64,AAAA",
      name: "工具结果",
      nested: { list: [1, 2, 3] },
    });
    expect(redacted).toEqual({
      blob: "data:image/png;base64,<base64 4 chars redacted>",
      name: "工具结果",
      nested: { list: [1, 2, 3] },
    });
  });
});
