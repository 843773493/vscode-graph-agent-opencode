import { describe, expect, test } from "bun:test";
import { boundedFailureText, failureMessage, formatFailureDiagnostic } from "@boxteam/plugin-sdk/failures";

describe("公共错误提取", () => {
  test("实际调用的小预算和负预算保持有界", () => {
    expect(boundedFailureText("中文正文", { maxBytes: -1 })).toBe("");
    for (const maxBytes of [0, 1, 2, 3, 5, 8]) {
      expect(new TextEncoder().encode(boundedFailureText("中文正文".repeat(10), { maxBytes })).byteLength).toBeLessThanOrEqual(maxBytes);
    }
  });
  test("保留未知原因并在裁剪前脱敏", () => {
    const secret = "operation-private-value";
    const error = new Error("真实原因 " + "x".repeat(8100) + secret, { cause: new Error("Authorization: Bearer " + secret) });
    const message = failureMessage(error, { secrets: [secret] });
    const diagnostic = formatFailureDiagnostic(error, { secrets: [secret] });
    expect(message.startsWith("真实原因")).toBe(true);
    expect(message).not.toContain(secret);
    expect(diagnostic).not.toContain(secret);
    expect(Buffer.byteLength(message)).toBeLessThanOrEqual(8192);
    expect(Buffer.byteLength(diagnostic)).toBeLessThanOrEqual(65536);
    expect(failureMessage(new Error())).toBe("未提供错误文本");
  });

  test("原因节点、深度与帧在构造时有界", () => {
    let reads = 0;
    const children = Array.from({ length: 1000 }, () => {
      const child = new Error();
      Object.defineProperty(child, "message", { get() { reads += 1; return "次生失败"; } });
      child.stack = "Error\n" + "    at frame (test.ts:1:1)\n".repeat(1000);
      return child;
    });
    const diagnostic = formatFailureDiagnostic(new AggregateError(children, "主失败"), { maxFrames: 3, maxBytes: 1024 });
    expect(reads).toBeLessThanOrEqual(8);
    expect(diagnostic).toContain("主失败");
    expect(diagnostic).toContain("已截断");
    expect(diagnostic.match(/    at /g)?.length).toBe(3);
    expect(Buffer.byteLength(diagnostic)).toBeLessThanOrEqual(1024);
  });
});
