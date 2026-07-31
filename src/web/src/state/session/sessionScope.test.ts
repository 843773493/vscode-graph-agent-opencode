import { describe, expect, test } from "bun:test";
import { parseSessionScopeKey, sessionScopeKey } from "./sessionScope";

describe("会话作用域键", () => {
  test("工作区名称包含特殊字符时仍可往返解析", () => {
    const key = sessionScopeKey("远程 workspace/一", "ses_123");

    expect(parseSessionScopeKey(key)).toEqual({
      workspaceId: "远程 workspace/一",
      sessionId: "ses_123",
    });
  });

  test("拒绝缺少工作区或会话 ID 的键", () => {
    expect(() => parseSessionScopeKey("invalid")).toThrow("会话作用域键格式无效");
    expect(() => parseSessionScopeKey("workspace::")).toThrow("会话作用域键格式无效");
  });
});
