import { afterEach, describe, expect, test } from "bun:test";
import { act } from "react-test-renderer";
import type { AppState } from "../../types/frontend";
import {
  apiResponse,
  installGatewayFetch,
  installTestDocument,
  installTestWindow,
  mountSessionGoalController,
  restoreSessionHookGlobals,
} from "./sessionHookTestFixtures";

afterEach(restoreSessionHookGlobals);

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
  installTestWindow(49_406);
  installTestDocument();
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
