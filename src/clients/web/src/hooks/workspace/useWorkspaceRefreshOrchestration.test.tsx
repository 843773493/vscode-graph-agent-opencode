import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as sessionRefresh from "../sessionEventStream/sessionRefresh";
import type { AppState } from "../../types/frontend";
import { useWorkspaceRefreshOrchestration } from "./useWorkspaceRefreshOrchestration";

const API_PORT = 49_811;

/** 只覆盖本链路读写的状态字段，其余字段对本链路无意义。 */
function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    workspaceSwitching: false,
    error: null,
    status: "",
    ...overrides,
  } as unknown as AppState;
}

interface MountedHook {
  hook: ReturnType<typeof useWorkspaceRefreshOrchestration>;
  current: () => AppState;
  aborts: number;
}

const renderers: ReactTestRenderer[] = [];
const restores: Array<() => void> = [];

async function mountHook(options: {
  initialState?: AppState;
  refreshSessions: (preferredSessionId?: string | null) => Promise<string | null>;
}): Promise<MountedHook> {
  let hook: ReturnType<typeof useWorkspaceRefreshOrchestration> | undefined;
  let current = options.initialState ?? appState();
  let aborts = 0;

  function Probe(): React.ReactNode {
    hook = useWorkspaceRefreshOrchestration({
      apiPort: API_PORT,
      setState: (update) => {
        current = typeof update === "function"
          ? (update as (previous: AppState) => AppState)(current)
          : update;
      },
      refreshSessions: options.refreshSessions,
      abortCurrentStream: () => {
        aborts += 1;
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  renderers.push(renderer!);
  return { hook: hook!, current: () => current, get aborts() { return aborts; } };
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

afterEach(() => {
  for (const renderer of renderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restores.splice(0)) restore();
});

describe("useWorkspaceRefreshOrchestration", () => {
  test("resetWorkspaceScopedState 先中止事件流再置切换态", async () => {
    const mounted = await mountHook({
      initialState: appState({ error: "旧错误", status: "旧状态" }),
      refreshSessions: async () => "workspace-a",
    });

    act(() => mounted.hook.resetWorkspaceScopedState());

    expect(mounted.aborts).toBe(1);
    expect(mounted.current().workspaceSwitching).toBe(true);
    expect(mounted.current().error).toBeNull();
    expect(mounted.current().status).toBe("正在切换工作区");
  });

  test("刷新被作废时返回 null 且不把 workspaceSwitching 收敛掉", async () => {
    const mounted = await mountHook({
      initialState: appState({ workspaceSwitching: true, error: "旧错误", status: "正在切换工作区" }),
      refreshSessions: async () => null,
    });

    let applied: string | null = "unset";
    await act(async () => {
      applied = await mounted.hook.finishWorkspaceRefresh("workspace-a", {
        checkGatewayWorkspaceHealth: false,
      });
    });

    expect(applied).toBeNull();
    // 作废的刷新没有任何工作区生效：切换态与旧错误必须原样保留，
    // 不能给出「工作区已就绪」的假成功。
    expect(mounted.current().workspaceSwitching).toBe(true);
    expect(mounted.current().error).toBe("旧错误");
    expect(mounted.current().status).toBe("正在切换工作区");
  });

  test("刷新生效时回传活动工作区 id 并收敛切换态", async () => {
    const mounted = await mountHook({
      initialState: appState({ workspaceSwitching: true, error: "旧错误" }),
      refreshSessions: async () => "workspace-b",
    });

    // 用持有者对象承接异步结果：直接给 let 变量赋值会被 TS 控制流收窄成
    // 初始值的字面量类型，这里需要保留真实的 string | null 联合。
    const result: { applied?: string | null } = {};
    await act(async () => {
      result.applied = await mounted.hook.finishWorkspaceRefresh();
    });

    expect(result.applied).toBe("workspace-b");
    expect(mounted.current().workspaceSwitching).toBe(false);
    expect(mounted.current().error).toBeNull();
    expect(mounted.current().status).toBe("工作区已就绪");
  });

  test("refreshGatewayWorkspaceSessions 以 force 强制刷新目标工作区列表", async () => {
    const calls: Array<{ apiPort: number; workspaceId: string | null; force: boolean }> = [];
    const spy = spyOn(sessionRefresh, "refreshWorkspaceSessionList").mockImplementation(
      async (apiPort, workspaceId, _setState, options = {}) => {
        calls.push({ apiPort, workspaceId, force: options.force ?? false });
      },
    );
    restores.push(() => spy.mockRestore());

    const mounted = await mountHook({ refreshSessions: async () => "workspace-a" });

    await act(async () => {
      await mounted.hook.refreshGatewayWorkspaceSessions("workspace-c");
    });
    await flush();

    expect(calls).toEqual([
      { apiPort: API_PORT, workspaceId: "workspace-c", force: true },
    ]);
  });
});
