import { describe, expect, test } from "bun:test";

import {
  LARGE_STRING_DISPLAY_LIMIT,
  JSON_DISPLAY_TEXT_LIMIT,
  boundedDisplayString,
  prettyJson,
  redactLargeData,
  serializeDisplayJson,
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

  test("不可序列化的对象属性省略，数组项按 JSON 规则转为 null", () => {
    const value = {
      omitUndefined: undefined,
      omitFunction: () => "not displayed",
      omitSymbol: Symbol("not displayed"),
      items: [undefined, () => "not displayed", Symbol("not displayed"), null],
    };

    const output = prettyJson(value);

    expect(JSON.parse(output)).toEqual({ items: [null, null, null, null] });
  });

  test("截断标记只生成一次后，后续深层数组截断不伪装成 null", () => {
    let firstBranch: unknown = { leaf: 1 };
    for (let depth = 0; depth < 99; depth += 1) {
      firstBranch = { child: firstBranch };
    }

    let secondBranch: unknown = [{ leaf: 1 }];
    for (let depth = 0; depth < 98; depth += 1) {
      secondBranch = [secondBranch];
    }

    const output = prettyJson([firstBranch, secondBranch]);
    const parsed = JSON.parse(output) as unknown[];

    expect(output).toContain("展示已截断");
    expect(output.split("…（展示已截断，后续内容未展示）")).toHaveLength(2);
    expect(JSON.stringify(parsed[1])).not.toContain("null");
  });

  test("大型数组和对象只保留有界节点并显式标注截断", () => {
    const largeArray = redactLargeData(Array.from({ length: 10_000 }, (_, index) => index));
    const largeObject = redactLargeData(
      Object.fromEntries(Array.from({ length: 10_000 }, (_, index) => [`field${index}`, index])),
    );
    const arrayResult = largeArray as unknown[];
    const objectResult = largeObject as Record<string, unknown>;

    expect(Array.isArray(largeArray)).toBe(true);
    expect(arrayResult).toHaveLength(5_000);
    expect(arrayResult[arrayResult.length - 1]).toContain("展示已截断");
    expect(Object.keys(objectResult)).toHaveLength(5_000);
    const objectValues = Object.values(objectResult);
    expect(objectValues[objectValues.length - 1]).toContain("展示已截断");
  });

  test("深层对象与整体文本预算耗尽时可见截断且不展开后续数据", () => {
    let deepValue: unknown = "leaf";
    for (let depth = 0; depth < 150; depth += 1) {
      deepValue = { child: deepValue };
    }

    const largeTextCollection = Array.from({ length: 20_000 }, () => "x".repeat(100));
    const deepOutput = prettyJson(deepValue);
    const largeTextOutput = prettyJson(largeTextCollection);

    expect(deepOutput).toContain("展示已截断");
    expect(largeTextOutput).toContain("展示已截断");
    expect(largeTextOutput.length).toBeLessThan(300_000);
  });

  test("自有 toJSON 方法不能绕过展示预算", () => {
    let invocationCount = 0;
    const value = {
      safe: "保留字段",
      toJSON() {
        invocationCount += 1;
        return "x".repeat(1_000_000);
      },
    };

    const output = prettyJson(value);

    expect(invocationCount).toBe(0);
    expect(output).toContain("保留字段");
    expect(output).not.toContain("x".repeat(100));
  });

  test("JSON 转义扩展与深层缩进也受最终展示文本上限约束", () => {
    const escapedKeys = Object.fromEntries(
      Array.from({ length: 4_000 }, (_, index) => [`${index}${"\0".repeat(40)}`, index]),
    );
    const deepBranches = Array.from({ length: 40 }, () => {
      let branch: unknown = "leaf";
      for (let depth = 0; depth < 99; depth += 1) {
        branch = { child: branch };
      }
      return branch;
    });

    const compactRedactedOutput = JSON.stringify(redactLargeData(escapedKeys)) ?? "";
    const compactOutput = serializeDisplayJson(escapedKeys);
    const prettyOutput = prettyJson(deepBranches);

    expect(compactRedactedOutput.length).toBeLessThanOrEqual(JSON_DISPLAY_TEXT_LIMIT);
    expect(compactOutput.length).toBeLessThanOrEqual(JSON_DISPLAY_TEXT_LIMIT);
    expect(compactOutput).toContain("展示已截断");
    expect(prettyOutput.length).toBeLessThanOrEqual(JSON_DISPLAY_TEXT_LIMIT);
    expect(prettyOutput).toContain("展示已截断");
    expect(() => JSON.parse(prettyOutput)).not.toThrow();
  });

  test("嵌套对象的截断标记计入紧凑和缩进输出预算", () => {
    let value: unknown = "leaf";
    for (let depth = 0; depth < 100; depth += 1) {
      value = { child: value, tail: "x".repeat(20_000) };
    }

    const compactOutput = JSON.stringify(redactLargeData(value)) ?? "";
    const prettyOutput = prettyJson(value);

    expect(compactOutput.length).toBeLessThanOrEqual(JSON_DISPLAY_TEXT_LIMIT);
    expect(prettyOutput.length).toBeLessThanOrEqual(JSON_DISPLAY_TEXT_LIMIT);
    expect(compactOutput).toContain("展示已截断");
    expect(prettyOutput).toContain("展示已截断");
    expect(() => JSON.parse(compactOutput)).not.toThrow();
    expect(() => JSON.parse(prettyOutput)).not.toThrow();
  });
});
