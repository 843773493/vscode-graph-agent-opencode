import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act } from "react-test-renderer";
import type { AppState } from "../../types/frontend";
import type { Session } from "../../types/backend";
import {
  apiResponse,
  installGatewayFetch,
  installTestDocument,
  installTestWindow,
  mountHarness,
  mountSessionGoalController,
  restoreSessionHookGlobals,
  useGoalControllerHarness,
} from "./sessionHookTestFixtures";

afterEach(restoreSessionHookGlobals);

const GOAL_PORT = 49_406;

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

/** 会话目标控制器的 GB 级装配由 mountSessionGoalController 统一提供。 */
function installBrowserGlobals(): void {
  installTestWindow(GOAL_PORT);
  installTestDocument();
}

/** 标准 Goal 响应体，用例只覆盖自己关心的字段。 */
function goalResponse(overrides: Record<string, unknown> = {}): Response {
  return apiResponse({
    goal_id: "goal-test",
    session_id: "session-test",
    objective: "验证请求合并",
    status: "active",
    token_budget: null,
    tokens_used: 0,
    time_used_seconds: 0,
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
    ...overrides,
  });
}

/** 挂载自持状态机的 Goal Harness，收敛「Harness + 闭包读写器」样板。 */
async function mountGoalHarness(options: {
  initial: AppState;
  flushes?: number;
}) {
  const harness = useGoalControllerHarness({
    apiPort: GOAL_PORT,
    initial: options.initial,
  });
  const unmount = await mountHarness(harness.Harness, options.flushes ?? 2);
  return { ...harness, unmount };
}

describe("useSessionGoalController 请求合并", () => {
  test("并发的初始读取、聚焦校准共享同一个 Goal 请求", async () => {
    installBrowserGlobals();
    let releaseGoal!: (response: Response) => void;
    let goalRequests = 0;
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        goalRequests += 1;
        return new Promise<Response>((resolve) => {
          releaseGoal = resolve;
        });
      }
      return undefined;
    }, { token: "local-test-token" });

    const { controller, unmount } = await mountSessionGoalController({
      initial: state(),
    });

    let firstRequest: Promise<unknown>;
    let secondRequest: Promise<unknown>;
    await act(async () => {
      firstRequest = controller().refreshGoal();
      secondRequest = controller().refreshGoal(undefined, { silent: true });
      await Promise.resolve();
    });
    expect(goalRequests).toBe(1);

    await act(async () => {
      releaseGoal(goalResponse());
      await Promise.all([firstRequest!, secondRequest!]);
    });
    unmount();
  });

  test("读取 Goal 失败时把原始错误文本写入 goalError", async () => {
    installBrowserGlobals();
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        throw new TypeError("读取 Goal 的网络请求失败");
      }
      return undefined;
    }, { token: "local-test-token" });

    const mounted = await mountSessionGoalController({ initial: state() });

    let failure: unknown;
    await act(async () => {
      failure = await mounted.controller().refreshGoal().catch((error: unknown) => error);
    });

    // 错误文案必须来自原始抛出物，而不是任何被换掉的常量占位。
    expect(mounted.state().goalError).toBe("读取 Goal 的网络请求失败");
    expect(failure).toBeInstanceOf(TypeError);
    mounted.unmount();
  });

  test("重取校准再次失败时把二次错误的原始文本拼进抛出的错误", async () => {
    installBrowserGlobals();
    let goalRequests = 0;
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        goalRequests += 1;
        throw new TypeError(goalRequests === 1 ? "首次写 Goal 失败" : "二次重取失败");
      }
      return undefined;
    }, { token: "local-test-token" });

    const { controller, unmount } = await mountSessionGoalController({
      initial: state(),
    });

    let failure: Error | undefined;
    await act(async () => {
      failure = await controller().updateGoal({ objective: "验证错误拼接" })
        .then(
          () => undefined,
          (error: Error) => error,
        );
    });

    expect(failure?.message).toBe("首次写 Goal 失败；重新读取 Goal 也失败：二次重取失败");
    unmount();
  });
});

describe("useSessionGoalController 跨会话守卫", () => {
  const sessionA = {
    session_id: "ses_a",
    workspace_id: "workspace-test",
    title: "会话 A",
    current_agent_id: "default",
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
  } as unknown as Session;
  const sessionB = { ...sessionA, session_id: "ses_b", title: "会话 B" };

  test("在途读取回包前切走会话时不得把旧 Goal 写进新会话", async () => {
    installBrowserGlobals();
    let releaseGoal!: (response: Response) => void;
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/ses_a/goal")) {
        return new Promise<Response>((resolve) => {
          releaseGoal = resolve;
        });
      }
      return undefined;
    }, { token: "gc-race-token" });

    // 本用例必须在请求在途期间切换 currentSession，因此保留自持状态机的装配器。
    const mounted = await mountGoalHarness({
      initial: {
        currentSession: sessionA,
        currentSessionWorkspaceId: "workspace-test",
        currentGoal: null,
        currentGoalSessionId: "ses_a",
        goalLoading: false,
        goalError: null,
      } as unknown as AppState,
    });

    let pending: Promise<unknown>;
    await act(async () => {
      pending = mounted.controller().refreshGoal({
        sessionId: "ses_a",
        workspaceId: "workspace-test",
      });
      await Promise.resolve();
    });
    await act(async () => {
      mounted.setState((prev) => ({
        ...prev,
        currentSession: sessionB,
        currentGoal: null,
        currentGoalSessionId: "ses_b",
      }));
      await Promise.resolve();
    });
    expect(mounted.state().currentSession?.session_id).toBe("ses_b");

    releaseGoal(goalResponse({ goal_id: "goal_a", objective: "A 的目标" }));
    await act(async () => {
      await pending!.catch(() => undefined);
    });

    // 迟到的 A 会话响应属于旧会话事实，绝不能污染已经切到的 B 会话。
    expect(mounted.state().currentSession?.session_id).toBe("ses_b");
    expect(mounted.state().currentGoal).toBeNull();
    expect(mounted.state().currentGoalSessionId).toBe("ses_b");
    mounted.unmount();
  });

  test("先设置后清除 Goal 时，迟到的设置回包不得复活已清除的 Goal", async () => {
    installBrowserGlobals();
    let releaseUpdate!: () => void;
    installGatewayFetch(({ path, method }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        if (method === "DELETE") {
          return apiResponse({ cleared: true });
        }
        if (method === "POST" || method === "PATCH" || method === "PUT") {
          return new Promise<Response>((resolve) => {
            releaseUpdate = () => resolve(
              goalResponse({ goal_id: "goal_late", objective: "迟到的设置" }),
            );
          });
        }
      }
      return undefined;
    }, { token: "goal-order-token" });

    const { controller, state: readState, unmount } = await mountSessionGoalController({
      initial: state(),
    });

    let updateRequest: Promise<unknown>;
    await act(async () => {
      updateRequest = controller().updateGoal({ objective: "迟到的设置" })
        .catch(() => undefined);
      await Promise.resolve();
    });
    await act(async () => {
      await controller().clearGoal();
    });
    releaseUpdate();
    await act(async () => {
      await updateRequest!;
    });

    // 用户最后一次操作是清除：迟到的设置回包不得把它复活。
    expect(readState().currentGoal).toBeNull();
    unmount();
  });
});

describe("useSessionGoalController 会话身份守卫", () => {
  test("目标会话为空时 refreshGoal 清空残留 Goal 状态并短路，绝不发请求", async () => {
    installBrowserGlobals();
    let goalRequests = 0;
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/")) {
        goalRequests += 1;
        return apiResponse({});
      }
      return undefined;
    }, { token: "gc-empty-target-token" });

    // 切到空会话时 AppState 里可能仍残留上一个会话的 Goal；refreshGoal 收到
    // sessionless 目标必须把它清干净，装配器保留注入残留状态的能力。
    const mounted = await mountGoalHarness({
      initial: {
        currentSession: null,
        currentSessionWorkspaceId: null,
        currentGoal: null,
        currentGoalSessionId: null,
        goalLoading: false,
        goalError: null,
      } as unknown as AppState,
      flushes: 1,
    });

    await act(async () => {
      mounted.setState((prev) => ({
        ...prev,
        // 模拟上一个会话残留的 Goal 与错误文本。
        currentGoal: {
          goal_id: "goal_stale",
          session_id: "ses_stale",
          objective: "上一个会话的目标",
          status: "active",
          token_budget: null,
          tokens_used: 0,
          time_used_seconds: 0,
          created_at: "2026-09-02T00:00:00Z",
          updated_at: "2026-09-02T00:00:00Z",
        } as never,
        currentGoalSessionId: "ses_stale",
        goalError: "上一个会话的错误",
      }));
      await Promise.resolve();
    });
    expect(mounted.state().currentGoal?.objective).toBe("上一个会话的目标");

    let result: unknown = "unset";
    await act(async () => {
      result = await mounted.controller().refreshGoal({ sessionId: "", workspaceId: null });
    });

    // sessionless 目标必须清空 Goal 状态并短路返回 null，绝不能残留上一个会话。
    expect(result).toBeNull();
    expect(mounted.state().currentGoal).toBeNull();
    expect(mounted.state().currentGoalSessionId).toBeNull();
    expect(mounted.state().goalLoading).toBe(false);
    expect(mounted.state().goalError).toBeNull();
    // 短路必须在任何网络请求之前发生。
    expect(goalRequests).toBe(0);
    mounted.unmount();
  });
});

