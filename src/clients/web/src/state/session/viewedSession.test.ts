import { afterEach, describe, expect, test } from "bun:test";
import type { AppState } from "../../types/frontend";
import { isSessionActivelyViewed } from "./viewedSession";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

function installDocument(visible: boolean, focused: boolean): void {
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      visibilityState: visible ? "visible" : "hidden",
      hasFocus: () => focused,
    },
  });
}

function state(overrides: Partial<AppState> = {}): AppState {
  return {
    currentSession: { session_id: "session-current" },
    currentSessionWorkspaceId: "workspace-a",
    ...overrides,
  } as unknown as AppState;
}

afterEach(() => {
  restoreGlobalDescriptor("document", originalDocumentDescriptor);
});

describe("isSessionActivelyViewed", () => {
  test("当前会话可见且窗口聚焦时才算正在查看", () => {
    installDocument(true, true);
    expect(isSessionActivelyViewed(state(), "workspace-a::session-current")).toBe(true);
  });

  test("页面不可见或窗口未聚焦时不算正在查看", () => {
    installDocument(false, true);
    expect(isSessionActivelyViewed(state(), "workspace-a::session-current")).toBe(false);
    installDocument(true, false);
    expect(isSessionActivelyViewed(state(), "workspace-a::session-current")).toBe(false);
  });

  test("不是当前会话或属于别的工作区时不算正在查看", () => {
    installDocument(true, true);
    expect(isSessionActivelyViewed(state(), "workspace-a::session-other")).toBe(false);
    expect(isSessionActivelyViewed(state(), "workspace-b::session-current")).toBe(false);
    expect(isSessionActivelyViewed(state({ currentSession: null }), "workspace-a::session-current"))
      .toBe(false);
    expect(isSessionActivelyViewed(
      state({ currentSessionWorkspaceId: null }),
      "workspace-a::session-current",
    )).toBe(false);
  });

  test("无 document 环境按正在查看处理，与对账链路原有兜底一致", () => {
    Reflect.deleteProperty(globalThis, "document");
    expect(isSessionActivelyViewed(state(), "workspace-a::session-current")).toBe(true);
  });
});

