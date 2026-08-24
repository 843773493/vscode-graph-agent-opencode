import { describe, expect, test } from "bun:test";
import {
  selectBootstrapSessionId,
  selectBootstrapToolDetailsExpanded,
  canAcceptUserViewStateResponse,
  canAcceptUserViewStateMutation,
} from "./useWorkspaceBootstrap";

describe("selectBootstrapSessionId", () => {
  test("用户切换后不沿用上一个用户的当前会话", () => {
    expect(selectBootstrapSessionId({
      preferredSessionId: "session-a",
      previousSessionId: "session-a",
      userChanged: true,
    })).toBeNull();
  });

  test("优先使用用户服务端保存的会话位置", () => {
    expect(selectBootstrapSessionId({
      persistedSessionId: "session-b",
      previousSessionId: "session-a",
      userChanged: true,
    })).toBe("session-b");
  });

  test("同一用户刷新时保留当前会话", () => {
    expect(selectBootstrapSessionId({
      previousSessionId: "session-a",
      userChanged: false,
    })).toBe("session-a");
  });

  test("切换用户时使用服务端保存的工具详情状态", () => {
    expect(selectBootstrapToolDetailsExpanded({
      persistedToolDetailsExpanded: true,
      previousToolDetailsExpanded: false,
      userChanged: true,
    })).toBe(true);
    expect(selectBootstrapToolDetailsExpanded({
      persistedToolDetailsExpanded: false,
      previousToolDetailsExpanded: true,
      userChanged: true,
    })).toBe(false);
  });

  test("同一用户刷新时不覆盖当前内存中的工具详情状态", () => {
    expect(selectBootstrapToolDetailsExpanded({
      persistedToolDetailsExpanded: false,
      previousToolDetailsExpanded: true,
      userChanged: false,
    })).toBe(true);
  });
});

describe("canAcceptUserViewStateResponse", () => {
  test("只接受当前用户的异步视图状态响应", () => {
    expect(canAcceptUserViewStateResponse("user-a", "user-a")).toBe(true);
    expect(canAcceptUserViewStateResponse("user-b", "user-a")).toBe(false);
    expect(canAcceptUserViewStateResponse(null, "user-a")).toBe(false);
  });
});

describe("canAcceptUserViewStateMutation", () => {
  test("接管后同一用户 ID 的旧请求结果也必须丢弃", () => {
    expect(canAcceptUserViewStateMutation({
      currentUserId: "alice",
      responseUserId: "alice",
      currentLeaseGeneration: 2,
      requestLeaseGeneration: 1,
    })).toBe(false);
  });

  test("同一租约代数的当前用户请求结果可以接受", () => {
    expect(canAcceptUserViewStateMutation({
      currentUserId: "alice",
      responseUserId: "alice",
      currentLeaseGeneration: 2,
      requestLeaseGeneration: 2,
    })).toBe(true);
  });
});
