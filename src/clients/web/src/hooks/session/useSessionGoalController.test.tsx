import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState } from "../../types/frontend";
import { useSessionGoalController } from "./useSessionGoalController";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

function installBrowserGlobals(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: "49406" },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      visibilityState: "visible",
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

function restoreGlobal(name: "window" | "document", descriptor?: PropertyDescriptor): void {
  if (descriptor) {
    Object.defineProperty(globalThis, name, descriptor);
  } else {
    Reflect.deleteProperty(globalThis, name);
  }
}

function apiResponse(data: unknown): Response {
  return new Response(JSON.stringify({
    code: 0,
    message: "ok",
    data,
    request_id: "request-test",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function state(): AppState {
  return {
    currentSession: {
      session_id: "session-test",
      workspace_id: "workspace-test",
      title: "测试会话",
      current_agent_id: "default",
      created_at: "2026-09-02T00:00:00Z",
      updated_at: "2026-09-02T00:00:00Z",
    },
    currentSessionWorkspaceId: "workspace-test",
    currentGoal: null,
    currentGoalSessionId: null,
    goalLoading: false,
    goalError: null,
  } as unknown as AppState;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  restoreGlobal("window", originalWindowDescriptor);
  restoreGlobal("document", originalDocumentDescriptor);
});

describe("useSessionGoalController 请求合并", () => {
  test("并发的初始读取、聚焦校准共享同一个 Goal 请求", async () => {
    installBrowserGlobals();
    let releaseGoal!: (response: Response) => void;
    let goalRequests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/v1/sessions/session-test/goal")) {
        goalRequests += 1;
        return await new Promise<Response>((resolve) => {
          releaseGoal = resolve;
        });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    let latestController: ReturnType<typeof useSessionGoalController> | null = null;
    function Harness(): React.ReactNode {
      const [currentState, setState] = React.useState(state);
      latestController = useSessionGoalController({
        apiPort: 49_406,
        currentSessionId: currentState.currentSession?.session_id ?? null,
        currentWorkspaceId: currentState.currentSessionWorkspaceId,
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });

    let firstRequest: Promise<unknown>;
    let secondRequest: Promise<unknown>;
    await act(async () => {
      firstRequest = latestController!.refreshGoal();
      secondRequest = latestController!.refreshGoal(undefined, { silent: true });
      await Promise.resolve();
    });
    expect(goalRequests).toBe(1);

    await act(async () => {
      releaseGoal(apiResponse({
        goal_id: "goal-test",
        session_id: "session-test",
        objective: "验证请求合并",
        status: "active",
        token_budget: null,
        tokens_used: 0,
        time_used_seconds: 0,
        created_at: "2026-09-02T00:00:00Z",
        updated_at: "2026-09-02T00:00:00Z",
      }));
      await Promise.all([firstRequest!, secondRequest!]);
    });
    act(() => renderer!.unmount());
  });

  test("读取 Goal 失败时把原始错误文本写入 goalError", async () => {
    installBrowserGlobals();
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/v1/sessions/session-test/goal")) {
        throw new TypeError("读取 Goal 的网络请求失败");
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    let latestController: ReturnType<typeof useSessionGoalController> | null = null;
    let latestState: AppState = state();
    function Harness(): React.ReactNode {
      const [currentState, setState] = React.useState(state);
      latestState = currentState;
      latestController = useSessionGoalController({
        apiPort: 49_406,
        currentSessionId: currentState.currentSession?.session_id ?? null,
        currentWorkspaceId: currentState.currentSessionWorkspaceId,
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });

    let failure: unknown;
    await act(async () => {
      failure = await latestController!.refreshGoal().catch((error: unknown) => error);
    });

    // 错误文案必须来自原始抛出物，而不是任何被换掉的常量占位。
    expect(latestState.goalError).toBe("读取 Goal 的网络请求失败");
    expect(failure).toBeInstanceOf(TypeError);
    act(() => renderer!.unmount());
  });

  test("重取校准再次失败时把二次错误的原始文本拼进抛出的错误", async () => {
    installBrowserGlobals();
    let goalRequests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/v1/sessions/session-test/goal")) {
        goalRequests += 1;
        throw new TypeError(goalRequests === 1 ? "首次写 Goal 失败" : "二次重取失败");
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    let latestController: ReturnType<typeof useSessionGoalController> | null = null;
    function Harness(): React.ReactNode {
      const [currentState, setState] = React.useState(state);
      latestController = useSessionGoalController({
        apiPort: 49_406,
        currentSessionId: currentState.currentSession?.session_id ?? null,
        currentWorkspaceId: currentState.currentSessionWorkspaceId,
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });

    let failure: Error | undefined;
    await act(async () => {
      failure = await latestController!.updateGoal({ objective: "验证错误拼接" })
        .then(
          () => undefined,
          (error: Error) => error,
        );
    });

    expect(failure?.message).toBe("首次写 Goal 失败；重新读取 Goal 也失败：二次重取失败");
    act(() => renderer!.unmount());
  });
});
