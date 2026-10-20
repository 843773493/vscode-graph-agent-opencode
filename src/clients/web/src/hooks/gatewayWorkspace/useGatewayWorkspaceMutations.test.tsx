import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as gatewayApi from "../../gatewayApi";
import { DEFAULT_BACKEND_PORT } from "../../api";
import type {
  GatewayWorkspace,
  GatewayWorkspaceList,
  Session,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { HttpRequestError } from "../../api/http";
import { registerWorkspaceObservation } from "../workspace/workspaceObservationLifecycle";
import { useGatewayWorkspaceMutations } from "./useGatewayWorkspaceMutations";
import { createSerialTaskQueue } from "../runtime/serialTaskQueue";

const API_PORT = 49_611;

/** 覆盖 remove 链路里被读写的两个 UI 设置块，键名与 remove 测试用的工作区 ID 对齐。 */
const fakeUiSettings = {
  session_sidebar: {
    collapsed_workspace_ids: ["ws-active", "ws-other"],
    expanded_root_tree_ids: ["workspace:ws-active", "workspace:ws-other"],
  },
  workspace_file_tree: {
    expanded_paths_by_workspace: {
      "ws-active": ["boxteam://workspace/ws-active/resources/files/root"],
      "ws-other": ["boxteam://workspace/ws-other/resources/files/root"],
    },
  },
} as unknown as WebUiSettings;

function gatewayWorkspace(
  overrides: Partial<GatewayWorkspace> = {},
): GatewayWorkspace {
  return {
    workspace_id: "ws-1",
    parent_workspace_id: null,
    name: "工作区一",
    backend_url: "http://127.0.0.1:41000",
    connection_kind: "local",
    status: "ready",
    active: false,
    managed: true,
    affected_workspace_ids: [],
    removable: true,
    system_default: false,
    runtime_action: "safe_restart_managed_backend",
    remote: null,
    services: {},
    connection_error: null,
    checked_at: "2026-09-10T00:00:00Z",
    ...overrides,
  };
}

function workspaceList(
  activeWorkspaceId: string | null,
  items: GatewayWorkspace[],
): GatewayWorkspaceList {
  return { active_workspace_id: activeWorkspaceId, items };
}

function session(sessionId: string): Session {
  return {
    session_id: sessionId,
    workspace_id: "ws_local",
    resource_identity: `fixture-${sessionId}`,
    uri: `boxteam://workspace/ws_local/resources/session/${sessionId}`,
    scope: "workspace",
    title: "测试会话",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  };
}

function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    activeGatewayWorkspaceId: "ws-active",
    gatewayWorkspaces: [],
    sessionsByWorkspace: new Map(),
    sessionGatewayWorkspaceById: new Map(),
    removingGatewayWorkspaceIds: new Set(),
    sessionHistoryReloadNonce: 0,
    workspaceSwitching: false,
    gatewayError: null,
    gatewayWorkspacesStale: false,
    error: null,
    workspaceName: "状态中的工作区",
    sessions: [],
    currentSession: null,
    currentSessionWorkspaceId: null,
    traceEvents: [],
    llmRequestLogs: [],
    sessionResources: [],
    agentStateJsonl: "{}",
    agentStateMessageCount: 3,
    status: "",
    isBootstrapping: true,
    ...overrides,
  } as unknown as AppState;
}

interface MountOptions {
  activeGatewayWorkspaceId?: string | null;
  finishWorkspaceRefresh?: (
    preferredSessionId?: string | null,
    refs?: {
      gatewayListRequestRef: { current: number };
      selectionIntentRef: { current: number };
      setState: (update: (previous: AppState) => AppState) => void;
    },
  ) => Promise<string | null>;
  resetWorkspaceScopedState?: () => void;
  abortCurrentStream?: () => void;
  updateUiSettings?: (
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ) => Promise<void>;
}

interface MountedHook {
  hook: ReturnType<typeof useGatewayWorkspaceMutations>;
  state: () => AppState;
  setState: (update: (previous: AppState) => AppState) => void;
  gatewayListRequestRef: { current: number };
  selectionIntentRef: { current: number };
  calls: {
    abort: number;
    invalidate: number;
    finish: number;
    reset: number;
    updates: WebUiSettingsUpdate[];
  };
}

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];

async function mountHook(
  initialState: AppState,
  options: MountOptions = {},
): Promise<MountedHook> {
  let current = initialState;
  let hook: ReturnType<typeof useGatewayWorkspaceMutations> | undefined;
  const calls: MountedHook["calls"] = {
    abort: 0,
    invalidate: 0,
    finish: 0,
    reset: 0,
    updates: [],
  };
  const selectionIntentRef = { current: 0 };
  const gatewayListRequestRef = { current: 0 };
  const queue = createSerialTaskQueue();
  const setState = (update: (previous: AppState) => AppState) => {
    current = update(current);
  };

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceMutations({
      selectionIntentRef,
      gatewayListRequestRef,
      runGatewayWorkspaceListTask: queue.enqueue,
      apiPort: API_PORT,
      activeGatewayWorkspaceId:
        options.activeGatewayWorkspaceId ?? initialState.activeGatewayWorkspaceId,
      setState: (update) => {
        current = typeof update === "function" ? update(current) : update;
      },
      abortCurrentStream: () => {
        calls.abort += 1;
        options.abortCurrentStream?.();
      },
      invalidateWorkspaceRefreshes: () => {
        calls.invalidate += 1;
      },
      finishWorkspaceRefresh: async (preferredSessionId) => {
        calls.finish += 1;
        if (!options.finishWorkspaceRefresh) {
          const refreshedState = current;
          gatewayListRequestRef.current += 1;
          if (refreshedState.gatewayWorkspacesStale !== false) return null;
          const activeWorkspace = refreshedState.gatewayWorkspaces.find(
            (workspace) => workspace.workspace_id === refreshedState.activeGatewayWorkspaceId,
          );
          setState((previous) => ({
            ...previous,
            gatewayWorkspaces: refreshedState.gatewayWorkspaces,
            activeGatewayWorkspaceId: refreshedState.activeGatewayWorkspaceId,
            ...(activeWorkspace
              ? {
                  workspaceName: activeWorkspace.name,
                }
              : {}),
            gatewayWorkspacesStale: false,
          }));
          return refreshedState.activeGatewayWorkspaceId;
        }
        return await options.finishWorkspaceRefresh(preferredSessionId, {
          gatewayListRequestRef,
          selectionIntentRef,
          setState,
        });
      },
      resetWorkspaceScopedState: () => {
        calls.reset += 1;
        options.resetWorkspaceScopedState?.();
      },
      updateUiSettings: async (input) => {
        const update = typeof input === "function"
          ? input(fakeUiSettings)
          : input;
        calls.updates.push(update);
        await options.updateUiSettings?.(update);
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);
  return {
    hook: hook!,
    state: () => current,
    setState: (update) => { current = update(current); },
    gatewayListRequestRef,
    selectionIntentRef,
    calls,
  };
}

function spyOnGatewayApi<Name extends keyof typeof gatewayApi>(
  name: Name,
): ReturnType<typeof spyOn<typeof gatewayApi, Name>> {
  const spy = spyOn(gatewayApi, name);
  restoreSpies.push(() => spy.mockRestore());
  return spy;
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restoreSpies.splice(0)) restore();
});

describe("Gateway 工作区排序", () => {
  test("活动工作区为空时清空原工作区身份与元数据", async () => {
    const reorder = spyOnGatewayApi("reorderGatewayWorkspaces").mockResolvedValue(
      workspaceList(null, [
        gatewayWorkspace({ workspace_id: "ws-1", name: "工作区一" }),
        gatewayWorkspace({ workspace_id: "ws-2", name: "工作区二" }),
      ]),
    );
    const list = spyOnGatewayApi("listGatewayWorkspaces");
    const { hook, state } = await mountHook(appState({
      activeGatewayWorkspaceId: "ws-fallback",
      workspaceName: "回退工作区",
    }));

    await hook.reorderGatewayWorkspaces(["ws-2", "ws-1"]);

    expect(reorder).toHaveBeenCalledWith(API_PORT, { workspace_ids: ["ws-2", "ws-1"] });
    expect(list).not.toHaveBeenCalled();
    const next = state();
    expect(next.activeGatewayWorkspaceId).toBeNull();
    expect(next.workspaceName).toBeNull();
    expect(next.gatewayWorkspaces.map((item) => item.workspace_id)).toEqual(["ws-1", "ws-2"]);
    expect(next.status).toBe("工作区顺序已更新");
  });

  test("排序失败后读取权威完整列表，反馈失败且原样抛出排序错误", async () => {
    const failure = new Error("排序后端不可用");
    const reorder = spyOnGatewayApi("reorderGatewayWorkspaces").mockRejectedValue(failure);
    const authoritativeList = workspaceList("ws-authoritative", [
      gatewayWorkspace({ workspace_id: "ws-authoritative", name: "权威工作区", }),
      gatewayWorkspace({ workspace_id: "ws-second", name: "第二个工作区" }),
    ]);
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(authoritativeList);
    const { hook, state } = await mountHook(appState({
      gatewayWorkspaces: [gatewayWorkspace({ workspace_id: "ws-old", name: "旧列表" })],
      gatewayWorkspacesStale: false,
    }));

    await expect(hook.reorderGatewayWorkspaces(["ws-2", "ws-1"])).rejects.toBe(failure);

    expect(reorder).toHaveBeenCalledTimes(1);
    expect(reorder).toHaveBeenCalledWith(API_PORT, { workspace_ids: ["ws-2", "ws-1"] });
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith(API_PORT);
    const next = state();
    expect(next.gatewayWorkspaces).toBe(authoritativeList.items);
    expect(next.gatewayWorkspacesStale).toBe(false);
    expect(next.activeGatewayWorkspaceId).toBe("ws-authoritative");
    expect(next.workspaceName).toBe("权威工作区");
    expect(next.gatewayError).toBe("排序后端不可用");
    expect(next.error).toBeNull();
    expect(next.status).toBe("工作区排序失败: 排序后端不可用");
  });

  test("排序和权威列表读取都失败时标记 stale、保留两种原因并原样抛出排序错误", async () => {
    const failure = new Error("排序提交结果未知");
    const readFailure = new Error("列表读取超时");
    const reorder = spyOnGatewayApi("reorderGatewayWorkspaces").mockRejectedValue(failure);
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(readFailure);
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-before" })];
    const { hook, state } = await mountHook(appState({
      gatewayWorkspaces: previousWorkspaces,
      gatewayWorkspacesStale: false,
    }));

    await expect(hook.reorderGatewayWorkspaces(["ws-2", "ws-1"])).rejects.toBe(failure);

    expect(reorder).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().gatewayError).toBe("排序提交结果未知；重新读取工作区列表也失败: 列表读取超时");
    expect(state().error).toBeNull();
    expect(state().status).toBe("工作区排序失败: 排序提交结果未知；重新读取工作区列表也失败: 列表读取超时");
  });

  test("旧排序失败的权威读回不能覆盖其后排队排序的完整结果", async () => {
    const failure = new Error("第一次排序被拒绝");
    const oldReadback = workspaceList("ws-old-readback", [
      gatewayWorkspace({ workspace_id: "ws-old-readback", name: "过期读回" }),
    ]);
    const latestList = workspaceList("ws-latest", [
      gatewayWorkspace({ workspace_id: "ws-latest", name: "最新排序" }),
      gatewayWorkspace({ workspace_id: "ws-second", name: "第二工作区" }),
    ]);
    let resolveReadback!: (value: GatewayWorkspaceList) => void;
    let resolveSecond!: (value: GatewayWorkspaceList) => void;
    const order: string[] = [];
    const reorder = spyOnGatewayApi("reorderGatewayWorkspaces")
      .mockImplementationOnce(() => {
        order.push("sort-first");
        return Promise.reject(failure);
      })
      .mockImplementationOnce(() => {
        order.push("sort-second");
        return new Promise((resolve) => { resolveSecond = resolve; });
      });
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => {
      order.push("readback-first");
      return new Promise((resolve) => { resolveReadback = resolve; });
    });
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-before" })];
    const { hook, state } = await mountHook(appState({
      gatewayWorkspaces: previousWorkspaces,
      gatewayWorkspacesStale: false,
    }));

    const first = hook.reorderGatewayWorkspaces(["ws-first", "ws-second"])
      .catch((error: unknown) => error);
    await act(async () => {
      for (let index = 0; index < 4; index += 1) await Promise.resolve();
    });
    expect(order).toEqual(["sort-first", "readback-first"]);
    expect(list).toHaveBeenCalledTimes(1);

    const second = hook.reorderGatewayWorkspaces(["ws-latest", "ws-second"]);
    await act(async () => { resolveReadback(oldReadback); });
    expect(await first).toBe(failure);
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().gatewayError).toBeNull();
    expect(order).toEqual(["sort-first", "readback-first", "sort-second"]);

    await act(async () => { resolveSecond(latestList); });
    await second;
    expect(reorder).toHaveBeenCalledTimes(2);
    expect(list).toHaveBeenCalledTimes(1);
    expect(state().gatewayWorkspaces).toBe(latestList.items);
    expect(state().activeGatewayWorkspaceId).toBe("ws-latest");
    expect(state().workspaceName).toBe("最新排序");
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().gatewayError).toBeNull();
    expect(state().status).toBe("工作区顺序已更新");
  });
});

describe("Gateway 工作区重命名", () => {
  test("失败重命名的补偿读取与错误反馈完成后，队列才执行后续成功重命名", async () => {
    const before = workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-active", name: "原名称" })]);
    const after = workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-active", name: "第二次名称" })]);
    const failure = new Error("第一次被拒绝");
    let rejectFirst!: (error: Error) => void;
    let resolveRead!: (value: GatewayWorkspaceList) => void;
    let resolveSecond!: (value: GatewayWorkspaceList) => void;
    const order: string[] = [];
    const rename = spyOnGatewayApi("renameGatewayWorkspace")
      .mockImplementationOnce(() => { order.push("rename-first"); return new Promise((_, reject) => { rejectFirst = reject; }); })
      .mockImplementationOnce(() => {
        order.push("rename-second");
        return new Promise((resolve) => { resolveSecond = resolve; });
      });
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => {
      order.push("reconcile-first");
      return new Promise((resolve) => { resolveRead = resolve; });
    });
    const { hook, state } = await mountHook(appState({
      gatewayWorkspaces: before.items,
      gatewayWorkspacesStale: false,
    }));
    const first = hook.renameGatewayWorkspace("ws-active", "第一次名称").catch((error: unknown) => error);
    const second = hook.renameGatewayWorkspace("ws-active", "第二次名称");
    await act(async () => { await Promise.resolve(); rejectFirst(failure); await Promise.resolve(); await Promise.resolve(); });
    expect(rename).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(order).toEqual(["rename-first", "reconcile-first"]);
    await act(async () => { resolveRead(workspaceList("ws-stale", [gatewayWorkspace({ workspace_id: "ws-stale" })])); });
    expect(await first).toBe(failure);
    expect(rename).toHaveBeenCalledTimes(2);
    expect(order).toEqual(["rename-first", "reconcile-first", "rename-second"]);
    expect(state().gatewayWorkspaces).toBe(before.items);
    expect(state().gatewayWorkspacesStale).toBe(false);
    await act(async () => { resolveSecond(after); });
    await second;
    expect(state().gatewayWorkspaces).toBe(after.items);
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().gatewayError).toBeNull();
    expect(state().status).toBe("工作区已重命名为「第二次名称」");
    expect(order).toEqual(["rename-first", "reconcile-first", "rename-second"]);
  });

  test("重命名响应缺少工作区时抛出明确错误并回滚读取工作区列表", async () => {
    const mutation = spyOnGatewayApi("renameGatewayWorkspace").mockResolvedValue(
      workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-2", name: "工作区二" })]),
    );
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
      workspaceList("ws-2", [gatewayWorkspace({ workspace_id: "ws-2", name: "工作区二", })]),
    );
    const { hook, state } = await mountHook(appState());

    await expect(hook.renameGatewayWorkspace("ws-1", "新名字")).rejects.toThrow(
      "Gateway 重命名响应缺少工作区: ws-1",
    );

    expect(mutation).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith(API_PORT);
    const next = state();
    expect(next.gatewayError).toBe("Gateway 重命名响应缺少工作区: ws-1");
    expect(next.status).toBe("重命名工作区失败: Gateway 重命名响应缺少工作区: ws-1");
    expect(next.activeGatewayWorkspaceId).toBe("ws-2");
    expect(next.workspaceName).toBe("工作区二");
  });

  test("重命名失败后重取工作区列表完成回滚", async () => {
    const failure = new Error("名称冲突");
    const mutation = spyOnGatewayApi("renameGatewayWorkspace").mockRejectedValue(failure);
    const authoritativeList = workspaceList("ws-active", [
      gatewayWorkspace({ workspace_id: "ws-active", name: "回滚后活动工作区", }),
    ]);
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(authoritativeList);
    const { hook, state } = await mountHook(appState({ gatewayWorkspacesStale: true }));

    await expect(hook.renameGatewayWorkspace("ws-1", "新名字")).rejects.toBe(failure);

    expect(mutation).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith(API_PORT);
    expect(state().gatewayWorkspaces).toBe(authoritativeList.items);
    expect(state().gatewayWorkspacesStale).toBe(false);
    const next = state();
    expect(next.gatewayError).toBe("名称冲突");
    expect(next.status).toBe("重命名工作区失败: 名称冲突");
    expect(next.workspaceName).toBe("回滚后活动工作区");
  });

  test("重命名失败且回滚也失败时拼接复合文案", async () => {
    const failure = new Error("名称冲突");
    const readFailure = new Error("列表不可用");
    const mutation = spyOnGatewayApi("renameGatewayWorkspace").mockRejectedValue(failure);
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(readFailure);
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-old" })];
    const { hook, state } = await mountHook(appState({
      gatewayWorkspaces: previousWorkspaces,
      gatewayWorkspacesStale: false,
    }));

    await expect(hook.renameGatewayWorkspace("ws-1", "新名字")).rejects.toBe(failure);

    expect(mutation).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith(API_PORT);
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().gatewayWorkspacesStale).toBe(true);
    const next = state();
    expect(next.gatewayError).toBe("名称冲突；重新读取工作区列表也失败: 列表不可用");
    expect(next.error).toBeNull();
    expect(next.status).toBe("重命名工作区失败: 名称冲突；重新读取工作区列表也失败: 列表不可用");
  });

  test("补偿读取在新选择意图后仍刷新列表，但不覆盖当前选择反馈", async () => {
    const failure = new Error("名称冲突");
    const authoritativeList = workspaceList("ws-reconciled", [
      gatewayWorkspace({ workspace_id: "ws-reconciled", name: "权威名称", }),
    ]);
    spyOnGatewayApi("renameGatewayWorkspace").mockRejectedValue(failure);
    let resolveRead!: (value: GatewayWorkspaceList) => void;
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(
      () => new Promise((resolve) => { resolveRead = resolve; }),
    );
    const { hook, state, selectionIntentRef } = await mountHook(appState({
      gatewayWorkspacesStale: false,
      gatewayError: "新选择的错误反馈",
      status: "新选择状态",
    }));
    const pending = hook.renameGatewayWorkspace("ws-1", "新名字").catch((error: unknown) => error);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(list).toHaveBeenCalledTimes(1);

    selectionIntentRef.current += 1;
    await act(async () => { resolveRead(authoritativeList); });

    expect(await pending).toBe(failure);
    expect(state().gatewayWorkspaces).toBe(authoritativeList.items);
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().activeGatewayWorkspaceId).toBe("ws-active");
    expect(state().workspaceName).toBe("状态中的工作区");
    expect(state().gatewayError).toBe("新选择的错误反馈");
    expect(state().status).toBe("新选择状态");
  });

  test("新重命名代次使在途旧成功响应失效", async () => {
    const oldWorkspaces = [gatewayWorkspace({ workspace_id: "ws-active", name: "旧列表" })];
    const firstList = workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-active", name: "第一次名称" })]);
    const secondList = workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-active", name: "第二次名称" })]);
    let resolveFirst!: (value: GatewayWorkspaceList) => void;
    let resolveSecond!: (value: GatewayWorkspaceList) => void;
    const order: string[] = [];
    const rename = spyOnGatewayApi("renameGatewayWorkspace")
      .mockImplementationOnce(() => {
        order.push("rename-first");
        return new Promise((resolve) => { resolveFirst = resolve; });
      })
      .mockImplementationOnce(() => {
        order.push("rename-second");
        return new Promise((resolve) => { resolveSecond = resolve; });
      });
    const list = spyOnGatewayApi("listGatewayWorkspaces");
    const { hook, state } = await mountHook(appState({ gatewayWorkspaces: oldWorkspaces }));
    const first = hook.renameGatewayWorkspace("ws-active", "第一次名称");
    const second = hook.renameGatewayWorkspace("ws-active", "第二次名称");
    await act(async () => { await Promise.resolve(); });
    expect(rename).toHaveBeenCalledTimes(1);

    await act(async () => { resolveFirst(firstList); });
    await first;
    expect(state().gatewayWorkspaces).toBe(oldWorkspaces);
    expect(rename).toHaveBeenCalledTimes(2);
    expect(order).toEqual(["rename-first", "rename-second"]);

    await act(async () => { resolveSecond(secondList); });
    await second;
    expect(list).not.toHaveBeenCalled();
    expect(state().gatewayWorkspaces).toBe(secondList.items);
    expect(state().gatewayWorkspaces[0]?.name).toBe("第二次名称");
  });
});

describe("Gateway 工作区删除", () => {
  test("删除非活动工作区不中断当前流，直接提前返回对账结果", async () => {
    const currentSession = session("ses-current");
    spyOnGatewayApi("removeGatewayWorkspace").mockResolvedValue(
      workspaceList("ws-active", [
        gatewayWorkspace({ workspace_id: "ws-active", name: "活动工作区" }),
      ]),
    );
    const { hook, state, calls } = await mountHook(appState({
      activeGatewayWorkspaceId: "ws-active",
      sessionsByWorkspace: new Map([["ws-other", [session("ses-other")]]]),
      currentSession,
      currentSessionWorkspaceId: "ws-active",
    }));

    await hook.removeGatewayWorkspace("ws-other");

    const next = state();
    expect(calls.abort).toBe(0);
    expect(calls.finish).toBe(0);
    expect(calls.invalidate).toBe(1);
    expect(next.status).toBe("工作区已删除");
    expect(next.workspaceSwitching).toBe(false);
    expect(next.currentSession).toBe(currentSession);
    expect(next.sessionsByWorkspace.has("ws-other")).toBe(false);
    expect([...next.removingGatewayWorkspaceIds]).toEqual([]);
    const update = calls.updates[calls.updates.length - 1] as unknown;
    expect(update).toEqual({
      session_sidebar: {
        collapsed_workspace_ids: ["ws-active"],
        expanded_root_tree_ids: ["workspace:ws-active"],
      },
      workspace_file_tree: {
        expanded_paths_by_workspace: { "ws-active": ["boxteam://workspace/ws-active/resources/files/root"] },
      },
    });
  });

  test("删除活动工作区清空会话级状态并标记正在切换", async () => {
    spyOnGatewayApi("removeGatewayWorkspace").mockResolvedValue(
      workspaceList("ws-2", [
        gatewayWorkspace({ workspace_id: "ws-2", name: "新活动工作区", }),
      ]),
    );
    const nextSession = session("ses-two");
    const { hook, state, calls } = await mountHook(appState({
      activeGatewayWorkspaceId: "ws-active",
      sessionsByWorkspace: new Map([
        ["ws-active", [session("ses-current")]],
        ["ws-2", [nextSession]],
      ]),
      currentSession: session("ses-current"),
      currentSessionWorkspaceId: "ws-active",
      traceEvents: [{ id: "trace" } as never],
      llmRequestLogs: [{ id: "log" } as never],
      sessionResources: [{ id: "resource" } as never],
      agentStateJsonl: "{\"a\":1}",
      agentStateMessageCount: 5,
    }));

    await hook.removeGatewayWorkspace("ws-active");

    expect(calls.abort).toBe(1);
    expect(calls.finish).toBe(1);
    const next = state();
    expect(next.activeGatewayWorkspaceId).toBe("ws-2");
    expect(next.workspaceSwitching).toBe(true);
    expect(next.workspaceName).toBe("新活动工作区");
    expect(next.sessions).toEqual([nextSession]);
    expect(next.currentSession).toBeNull();
    expect(next.currentSessionWorkspaceId).toBeNull();
    expect(next.traceEvents).toEqual([]);
    expect(next.llmRequestLogs).toEqual([]);
    expect(next.sessionResources).toEqual([]);
    expect(next.agentStateJsonl).toBe("");
    expect(next.agentStateMessageCount).toBe(0);
    expect([...next.removingGatewayWorkspaceIds]).toEqual([]);
  });

  test("删除成功但新活动工作区加载失败时不给文案加删除失败前缀", async () => {
    const refreshFailure = new Error("刷新失败");
    spyOnGatewayApi("removeGatewayWorkspace").mockResolvedValue(
      workspaceList("ws-2", [gatewayWorkspace({ workspace_id: "ws-2", name: "新活动工作区" })]),
    );
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
      workspaceList("ws-2", [gatewayWorkspace({ workspace_id: "ws-2", name: "新活动工作区" })]),
    );
    const { hook, state } = await mountHook(
      appState({ activeGatewayWorkspaceId: "ws-active" }),
      {
        finishWorkspaceRefresh: async (_preferredSessionId, refs) => {
          refs!.gatewayListRequestRef.current += 1;
          throw refreshFailure;
        },
      },
    );

    await expect(hook.removeGatewayWorkspace("ws-active")).rejects.toBe(refreshFailure);

    const message = "工作区已删除，但新活动工作区加载失败: 刷新失败";
    const next = state();
    expect(next.gatewayError).toBe(message);
    expect(next.error).toBeNull();
    expect(next.status).toBe(message);
    expect(next.status.startsWith("删除工作区失败")).toBe(false);
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
  });

  test("非活动工作区删除已提交但界面设置清理失败时保留提交事实并标记陈旧列表", async () => {
    const settingsFailure = new Error("设置写入失败");
    const reconciliationFailure = new Error("列表不可用");
    const committedList = workspaceList("ws-active", [
      gatewayWorkspace({ workspace_id: "ws-active", name: "仍活动的工作区" }),
    ]);
    const remove = spyOnGatewayApi("removeGatewayWorkspace").mockResolvedValue(committedList);
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(reconciliationFailure);
    const { hook, state, calls } = await mountHook(
      appState({
        activeGatewayWorkspaceId: "ws-active",
        gatewayWorkspaces: [
          gatewayWorkspace({ workspace_id: "ws-active" }),
          gatewayWorkspace({ workspace_id: "ws-other" }),
        ],
        gatewayWorkspacesStale: false,
      }),
      { updateUiSettings: async () => { throw settingsFailure; } },
    );

    await expect(hook.removeGatewayWorkspace("ws-other")).rejects.toBe(settingsFailure);

    const message =
      "工作区已删除，但本地界面设置清理失败: 设置写入失败；重新读取工作区列表也失败: 列表不可用";
    const next = state();
    expect(remove).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(calls.updates).toHaveLength(1);
    expect(calls.finish).toBe(0);
    expect(next.gatewayWorkspaces.map((item) => item.workspace_id)).toEqual(["ws-active"]);
    expect(next.activeGatewayWorkspaceId).toBe("ws-active");
    expect(next.gatewayError).toBe(message);
    expect(next.status).toBe(message);
    expect(next.gatewayWorkspacesStale).toBe(true);
    expect(next.workspaceSwitching).toBe(false);
    expect([...next.removingGatewayWorkspaceIds]).toEqual([]);
  });

  test("删除接口失败时重取工作区列表回滚并清理删除中标记", async () => {
    const failure = new Error("删除被拒绝");
    spyOnGatewayApi("removeGatewayWorkspace").mockRejectedValue(failure);
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
      workspaceList("ws-active", [
        gatewayWorkspace({ workspace_id: "ws-active", name: "回滚后的活动工作区" }),
        gatewayWorkspace({ workspace_id: "ws-other", name: "仍然存在的工作区" }),
      ]),
    );
    const { hook, state } = await mountHook(appState({
      removingGatewayWorkspaceIds: new Set(["ws-other", "ws-active"]),
    }));

    await expect(hook.removeGatewayWorkspace("ws-other")).rejects.toBe(failure);

    const next = state();
    expect(next.gatewayError).toBe("删除被拒绝");
    expect(next.error).toBeNull();
    expect(next.status).toBe("删除工作区失败: 删除被拒绝");
    expect(next.activeGatewayWorkspaceId).toBe("ws-active");
    expect(next.gatewayWorkspaces.map((item) => item.workspace_id)).toEqual(["ws-active", "ws-other"]);
    expect([...next.removingGatewayWorkspaceIds]).toEqual(["ws-active"]);
    expect(next.workspaceSwitching).toBe(false);
  });

  test("删除失败对账时工作区根路径与名称必须跟着活动工作区一起替换", async () => {
    // 对账把 activeGatewayWorkspaceId 换成回滚结果里的活动工作区后，若还留着旧
    // 的 workspaceName，文件树与预览就会按新工作区 id 去读旧根路径。
    spyOnGatewayApi("removeGatewayWorkspace").mockRejectedValue(new Error("删除被拒绝"));
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
      workspaceList("ws-other", [
        gatewayWorkspace({
          workspace_id: "ws-other",
          name: "回滚后的新活动工作区",
          }),
      ]),
    );
    const { hook, state } = await mountHook(appState({
      activeGatewayWorkspaceId: "ws-active",
      workspaceName: "陈旧的工作区",
    }));

    await expect(hook.removeGatewayWorkspace("ws-other")).rejects.toThrow("删除被拒绝");

    const next = state();
    expect(next.activeGatewayWorkspaceId).toBe("ws-other");
    expect(next.workspaceName).toBe("回滚后的新活动工作区");
  });

  test("删除接口与回滚列表同时失败时拼接复合文案并清理删除中标记", async () => {
    const failure = new Error("删除被拒绝");
    spyOnGatewayApi("removeGatewayWorkspace").mockRejectedValue(failure);
    spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(new Error("列表不可用"));
    const { hook, state } = await mountHook(appState({
      removingGatewayWorkspaceIds: new Set(["ws-other", "ws-active"]),
    }));

    await expect(hook.removeGatewayWorkspace("ws-other")).rejects.toBe(failure);

    const message = "删除被拒绝；重新读取工作区列表也失败: 列表不可用";
    const next = state();
    expect(next.gatewayError).toBe(message);
    expect(next.error).toBeNull();
    expect(next.status).toBe(`删除工作区失败: ${message}`);
    expect(next.gatewayWorkspacesStale).toBe(true);
    expect([...next.removingGatewayWorkspaceIds]).toEqual(["ws-active"]);
    expect(next.workspaceSwitching).toBe(false);
  });

  test("旧删除的失败回读晚于同工作区新删除开始时不覆盖列表或清除新标记", async () => {
    const failure = new Error("第一次删除被拒绝");
    const previousWorkspaces = [
      gatewayWorkspace({ workspace_id: "ws-active" }),
      gatewayWorkspace({ workspace_id: "ws-other", name: "当前列表" }),
    ];
    const firstRead = workspaceList("ws-active", [
      gatewayWorkspace({ workspace_id: "ws-active" }),
      gatewayWorkspace({ workspace_id: "ws-other", name: "迟到回读" }),
    ]);
    const nextList = workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-active" })]);
    let resolveRead!: (value: GatewayWorkspaceList) => void;
    const pendingRead = new Promise<GatewayWorkspaceList>((resolve) => { resolveRead = resolve; });
    let resolveSecondRemoval!: (value: GatewayWorkspaceList) => void;
    const pendingSecondRemoval = new Promise<GatewayWorkspaceList>((resolve) => {
      resolveSecondRemoval = resolve;
    });
    let removalCalls = 0;
    const remove = spyOnGatewayApi("removeGatewayWorkspace").mockImplementation(async () => {
      removalCalls += 1;
      if (removalCalls === 1) throw failure;
      return await pendingSecondRemoval;
    });
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(async () => {
      return await pendingRead;
    });
    const { hook, state } = await mountHook(appState({ gatewayWorkspaces: previousWorkspaces }));

    const first = hook.removeGatewayWorkspace("ws-other");
    void first.catch(() => undefined);
    await act(async () => {
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });
    expect(list).toHaveBeenCalledTimes(1);
    let second!: Promise<void>;
    await act(async () => {
      second = hook.removeGatewayWorkspace("ws-other");
    });

    expect(state().removingGatewayWorkspaceIds.has("ws-other")).toBe(true);
    await act(async () => { resolveRead(firstRead); });
    await expect(first).rejects.toBe(failure);
    await act(async () => {
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });

    expect(list).toHaveBeenCalledTimes(1);
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().gatewayWorkspaces[1]?.name).toBe("当前列表");
    expect(state().removingGatewayWorkspaceIds.has("ws-other")).toBe(true);
    expect(state().status).toBe("正在删除工作区；工作区 ws-other 删除失败: 第一次删除被拒绝");
    expect(remove).toHaveBeenCalledTimes(2);

    await act(async () => { resolveSecondRemoval(nextList); });
    await second;
    expect(state().gatewayWorkspaces).toBe(nextList.items);
    expect(state().removingGatewayWorkspaceIds.has("ws-other")).toBe(false);
  });

  test("活动工作区删除刷新等待期间的新列表代次拥有最终列表与反馈", async () => {
    const refreshFailure = new Error("旧刷新失败");
    const committedList = workspaceList("ws-next", [
      gatewayWorkspace({ workspace_id: "ws-next", name: "新活动工作区" }),
    ]);
    const latestList = workspaceList("ws-next", [
      gatewayWorkspace({ workspace_id: "ws-next", name: "最新工作区名称" }),
    ]);
    let rejectRefresh!: (error: Error) => void;
    const pendingRefresh = new Promise<string | null>((_resolve, reject) => { rejectRefresh = reject; });
    spyOnGatewayApi("removeGatewayWorkspace").mockResolvedValue(committedList);
    const rename = spyOnGatewayApi("renameGatewayWorkspace").mockResolvedValue(latestList);
    const list = spyOnGatewayApi("listGatewayWorkspaces");
    const { hook, state, gatewayListRequestRef, selectionIntentRef, calls } = await mountHook(
      appState({ activeGatewayWorkspaceId: "ws-active", gatewayWorkspacesStale: false }),
      {
        finishWorkspaceRefresh: async (_preferredSessionId, refs) => {
          refs!.gatewayListRequestRef.current += 1;
          return await pendingRefresh;
        },
      },
    );

    const removal = hook.removeGatewayWorkspace("ws-active");
    void removal.catch(() => undefined);
    await act(async () => {
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });
    expect(state().gatewayWorkspaces).toBe(committedList.items);
    expect(calls.finish).toBe(1);
    expect(gatewayListRequestRef.current).toBe(2);

    selectionIntentRef.current += 1;
    await hook.renameGatewayWorkspace("ws-next", "最新工作区名称");
    expect(rename).toHaveBeenCalledTimes(1);
    expect(gatewayListRequestRef.current).toBe(3);
    expect(state().gatewayWorkspaces).toBe(latestList.items);
    expect(state().status).toBe("工作区已重命名为「最新工作区名称」");

    await act(async () => { rejectRefresh(refreshFailure); });
    await expect(removal).rejects.toBe(refreshFailure);

    expect(list).not.toHaveBeenCalled();
    expect(state().gatewayWorkspaces).toBe(latestList.items);
    expect(state().activeGatewayWorkspaceId).toBe("ws-next");
    expect(state().gatewayError).toBeNull();
    expect(state().status).toBe(
      "工作区已重命名为「最新工作区名称」；工作区 ws-active 删除已提交，但新活动工作区加载失败: 旧刷新失败",
    );
    expect(state().gatewayWorkspacesStale).toBe(false);
  });

  test("仅选择意图变旧时失败回读保留新活动状态并追加工作区错误", async () => {
    const failure = new Error("删除被拒绝");
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-active" })];
    const reconciliationFailure = new Error("列表不可用");
    let rejectRemoval!: (error: Error) => void;
    const pendingRemoval = new Promise<GatewayWorkspaceList>((_resolve, reject) => {
      rejectRemoval = reject;
    });
    const remove = spyOnGatewayApi("removeGatewayWorkspace").mockImplementation(
      async () => await pendingRemoval,
    );
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(reconciliationFailure);
    const { hook, state, setState, selectionIntentRef, gatewayListRequestRef } = await mountHook(
      appState({
        activeGatewayWorkspaceId: "ws-active",
        gatewayWorkspaces: previousWorkspaces,
        gatewayWorkspacesStale: false,
      }),
    );

    const removal = hook.removeGatewayWorkspace("ws-other");
    void removal.catch(() => undefined);
    await act(async () => {
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });
    expect(remove).toHaveBeenCalledTimes(1);
    const generation = gatewayListRequestRef.current;
    selectionIntentRef.current += 1;
    setState((previous) => ({
      ...previous,
      activeGatewayWorkspaceId: "ws-selected",
      workspaceName: "选择中的工作区",
      gatewayError: "选择操作错误",
      error: "选择操作错误详情",
      status: "正在切换到选择中的工作区",
      workspaceSwitching: true,
      isBootstrapping: true,
    }));

    await act(async () => { rejectRemoval(failure); });
    await expect(removal).rejects.toBe(failure);

    expect(gatewayListRequestRef.current).toBe(generation);
    expect(list).toHaveBeenCalledTimes(1);
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().activeGatewayWorkspaceId).toBe("ws-selected");
    expect(state().workspaceName).toBe("选择中的工作区");
    expect(state().gatewayError).toBe("选择操作错误");
    expect(state().error).toBe("选择操作错误详情");
    expect(state().workspaceSwitching).toBe(true);
    expect(state().isBootstrapping).toBe(true);
    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().status).toBe(
      "正在切换到选择中的工作区；工作区 ws-other 删除失败: 删除被拒绝；重新读取工作区列表也失败: 列表不可用",
    );
  });
});

describe("Gateway 受管工作区新增", () => {
  const addPayload = { root_path: "/tmp/new-workspace" };

  beforeEach(() => {
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(workspaceList(
      "ws-active", [gatewayWorkspace({ workspace_id: "ws-active", active: true })],
    ));
  });

  test.each([null, "conn-1"])("添加原生目录只提交给管理入口，不写UI设置：%s", async (connectionId) => {
    const add = spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    const { hook, calls } = await mountHook(appState(), {
      updateUiSettings: async () => { throw new Error("不应把原生目录复制到UI设置"); },
    });
    const payload = { root_path: "/tmp/用户选择的目录", ...(connectionId ? { gateway_connection_id: connectionId } : {}) };

    await hook.addManagedGatewayWorkspace(payload);

    expect(add).toHaveBeenCalledTimes(1);
    expect(add.mock.calls[0]).toEqual([API_PORT, payload]);
    expect(calls.updates).toEqual([]);
    expect(calls.finish).toBe(1);
  });

  test("已添加工作区但刷新失败时仍报告真实错误", async () => {
    spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    const { hook, state, calls } = await mountHook(appState(), {
      finishWorkspaceRefresh: async () => { throw new Error("刷新失败"); },
    });

    const message = "工作区已添加，但界面同步失败: 刷新工作区列表失败: 刷新失败";
    await expect(hook.addManagedGatewayWorkspace(addPayload)).rejects.toThrow(message);

    const next = state();
    expect(next.gatewayError).toBe(message);
    expect(next.error).toBeNull();
    expect(next.status).toBe(message);
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
    expect(calls.updates).toEqual([]);
  });

  test("新增受管工作区接口失败时回读权威列表并原样抛出，活动工作区变化后刷新上下文", async () => {
    const failure = new Error("后端拒绝");
    spyOnGatewayApi("addManagedGatewayWorkspace").mockRejectedValue(failure);
    const refreshedWorkspaces = workspaceList("ws-new-active", [
      gatewayWorkspace({
        workspace_id: "ws-new-active",
        name: "新活动工作区",
        active: true,
      }),
    ]);
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(refreshedWorkspaces);
    const { hook, state, calls } = await mountHook(appState(), {
      finishWorkspaceRefresh: async (_preferredSessionId, refs) => {
        refs!.gatewayListRequestRef.current += 1;
        refs!.setState((previous) => ({
          ...previous,
          gatewayWorkspaces: refreshedWorkspaces.items,
          activeGatewayWorkspaceId: refreshedWorkspaces.active_workspace_id,
          workspaceName: "新活动工作区",
          gatewayWorkspacesStale: false,
        }));
        return refreshedWorkspaces.active_workspace_id;
      },
    });

    await expect(hook.addManagedGatewayWorkspace(addPayload)).rejects.toBe(failure);

    expect(list).toHaveBeenCalledTimes(1);
    const next = state();
    expect(next.gatewayError).toBe("后端拒绝");
    expect(next.error).toBeNull();
    expect(next.status).toBe("添加工作区失败: 后端拒绝");
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
    expect(next.gatewayWorkspaces).toBe(refreshedWorkspaces.items);
    expect(next.activeGatewayWorkspaceId).toBe("ws-new-active");
    expect(calls.finish).toBe(1);
    expect(calls.updates).toEqual([]);
  });

  test("添加失败且权威列表读取失败时保留原错误并标记列表失效", async () => {
    const failure = new Error("提交结果未知");
    spyOnGatewayApi("addManagedGatewayWorkspace").mockRejectedValue(failure);
    spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(new Error("完整列表不可用"));
    const { hook, state, calls } = await mountHook(appState({ gatewayWorkspacesStale: false }));

    await expect(hook.addManagedGatewayWorkspace(addPayload)).rejects.toBe(failure);

    expect(calls.finish).toBe(0);
    expect(calls.updates).toEqual([]);
    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().gatewayError).toBe("提交结果未知；读取 Gateway 工作区列表失败: 完整列表不可用");
    expect(state().status).toBe(
      "添加工作区失败: 提交结果未知；读取 Gateway 工作区列表失败: 完整列表不可用",
    );
  });

  test("活动工作区变化后的刷新失败只附加诊断并仍抛出原 POST 错误", async () => {
    const failure = new Error("后端拒绝");
    const refreshFailure = new Error("新工作区加载失败");
    spyOnGatewayApi("addManagedGatewayWorkspace").mockRejectedValue(failure);
    const authoritativeList = workspaceList("ws-new-active", [
      gatewayWorkspace({ workspace_id: "ws-new-active", }),
    ]);
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(authoritativeList);
    const { hook, state, calls } = await mountHook(appState(), {
      finishWorkspaceRefresh: async (_preferredSessionId, refs) => {
        refs!.gatewayListRequestRef.current += 1;
        throw refreshFailure;
      },
    });

    await expect(hook.addManagedGatewayWorkspace(addPayload)).rejects.toBe(failure);

    expect(calls.finish).toBe(1);
    expect(state().gatewayWorkspaces).toBe(authoritativeList.items);
    expect(state().activeGatewayWorkspaceId).toBe("ws-new-active");
    expect(state().gatewayError).toBe("后端拒绝；刷新工作区列表失败: 新工作区加载失败");
    expect(state().status).toBe(
      "添加工作区失败: 后端拒绝；刷新工作区列表失败: 新工作区加载失败",
    );
  });

  test("添加失败的回读晚于新列表代次时只追加目标诊断", async () => {
    const failure = new Error("提交结果未知");
    spyOnGatewayApi("addManagedGatewayWorkspace").mockRejectedValue(failure);
    let resolveList!: (value: GatewayWorkspaceList) => void;
    const pendingList = new Promise<GatewayWorkspaceList>((resolve) => { resolveList = resolve; });
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(async () => await pendingList);
    const { hook, state, setState, gatewayListRequestRef, calls } = await mountHook(
      appState({
        gatewayWorkspaces: [gatewayWorkspace({ workspace_id: "ws-active" })],
        gatewayWorkspacesStale: false,
      }),
    );

    const operation = hook.addManagedGatewayWorkspace(addPayload);
    void operation.catch(() => undefined);
    await act(async () => {
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });
    expect(list).toHaveBeenCalledTimes(1);
    gatewayListRequestRef.current += 1;
    setState((previous) => ({
      ...previous,
      gatewayWorkspaces: [gatewayWorkspace({ workspace_id: "ws-latest", name: "最新列表" })],
      activeGatewayWorkspaceId: "ws-latest",
      workspaceName: "最新列表",
      gatewayError: "更新操作错误",
      status: "更新操作状态",
    }));
    await act(async () => {
      resolveList(workspaceList("ws-stale", [gatewayWorkspace({ workspace_id: "ws-stale" })]));
    });
    await expect(operation).rejects.toBe(failure);

    expect(calls.finish).toBe(0);
    expect(state().gatewayWorkspaces[0]?.workspace_id).toBe("ws-latest");
    expect(state().activeGatewayWorkspaceId).toBe("ws-latest");
    expect(state().gatewayError).toBe("更新操作错误");
    expect(state().status).toContain("更新操作状态；工作区 root_path=\"/tmp/new-workspace\" 添加失败:");
    expect(state().status).toContain("当前列表请求或工作区选择已更新");
  });

  test("添加失败时仅选择意图变旧仍更新列表但不覆盖活动工作区状态", async () => {
    const failure = new Error("提交结果未知");
    let rejectAdd!: (error: Error) => void;
    const pendingAdd = new Promise<never>((_resolve, reject) => { rejectAdd = reject; });
    spyOnGatewayApi("addManagedGatewayWorkspace").mockImplementation(async () => await pendingAdd);
    const authoritativeList = workspaceList("ws-server-active", [
      gatewayWorkspace({ workspace_id: "ws-server-active", }),
    ]);
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(authoritativeList);
    const { hook, state, setState, selectionIntentRef, calls } = await mountHook(
      appState({ gatewayWorkspacesStale: false }),
    );

    const operation = hook.addManagedGatewayWorkspace(addPayload);
    void operation.catch(() => undefined);
    await act(async () => {
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });
    selectionIntentRef.current += 1;
    setState((previous) => ({
      ...previous,
      activeGatewayWorkspaceId: "ws-selected",
      workspaceName: "用户选择的工作区",
      gatewayError: "选择错误",
      status: "正在切换工作区",
      workspaceSwitching: true,
      isBootstrapping: true,
    }));
    await act(async () => { rejectAdd(failure); });
    await expect(operation).rejects.toBe(failure);

    expect(calls.finish).toBe(0);
    expect(state().gatewayWorkspaces).toBe(authoritativeList.items);
    expect(state().activeGatewayWorkspaceId).toBe("ws-selected");
    expect(state().workspaceName).toBe("用户选择的工作区");
    expect(state().gatewayError).toBe("选择错误");
    expect(state().workspaceSwitching).toBe(true);
    expect(state().isBootstrapping).toBe(true);
    expect(state().status).toContain("正在切换工作区；工作区 root_path=\"/tmp/new-workspace\" 添加失败:");
  });

  test("已提交后的列表读取失败只有刷新实际应用新状态时才解除诊断", async () => {
    const add = spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(new Error("完整列表不可用"));
    const refreshedWorkspaces = workspaceList("ws-active", [
      gatewayWorkspace({ workspace_id: "ws-active", name: "刷新后的活动工作区", active: true }),
    ]);
    const { hook, state, calls } = await mountHook(appState({ gatewayWorkspacesStale: false }), {
      finishWorkspaceRefresh: async (_preferredSessionId, refs) => {
        refs!.gatewayListRequestRef.current += 1;
        refs!.setState((previous) => ({
          ...previous,
          gatewayWorkspaces: refreshedWorkspaces.items,
          activeGatewayWorkspaceId: refreshedWorkspaces.active_workspace_id,
          workspaceName: "刷新后的活动工作区",
          gatewayWorkspacesStale: false,
        }));
        return refreshedWorkspaces.active_workspace_id;
      },
    });

    await hook.addManagedGatewayWorkspace(addPayload);

    expect(add).toHaveBeenCalledTimes(1);
    expect(calls.updates).toEqual([]);
    expect(calls.finish).toBe(1);
    expect(state().gatewayWorkspaces).toBe(refreshedWorkspaces.items);
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().gatewayError).toBeNull();
  });

  test("已提交后的列表读取失败且刷新未实际应用状态时保留读取与刷新诊断", async () => {
    spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(new Error("完整列表不可用"));
    const { hook, state, calls } = await mountHook(appState({ gatewayWorkspacesStale: false }));

    const message = "工作区已添加，但界面同步失败: 读取 Gateway 工作区列表失败: 完整列表不可用；刷新工作区列表未生效：本轮刷新已被更新的请求作废";
    await expect(hook.addManagedGatewayWorkspace(addPayload)).rejects.toThrow(message);

    expect(calls.finish).toBe(1);
    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().gatewayError).toBe(message);
    expect(state().status).toBe(message);
  });
});

describe("Gateway 远程工作区新增", () => {
  test("注册失败后回读权威列表、写入连接失败文案并原样抛出", async () => {
    const events: string[] = [];
    const failure = new Error("连接被拒绝");
    const add = spyOnGatewayApi("addSshGatewayWorkspace").mockImplementation(async () => {
      events.push("api");
      throw failure;
    });
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-old" })];
    const authoritativeList = workspaceList("ws-current", [
      gatewayWorkspace({ workspace_id: "ws-current", }),
    ]);
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(async () => {
      events.push("list");
      return authoritativeList;
    });
    const { hook, state, calls } = await mountHook(appState({
      gatewayWorkspaces: previousWorkspaces,
      gatewayWorkspacesStale: false,
    }), {
      resetWorkspaceScopedState: () => { events.push("reset"); },
    });

    await expect(hook.addSshGatewayWorkspace({
      remote_gateway_port: 8014,
      ssh_config_host: "remote-host",
    })).rejects.toBe(failure);

    expect(events).toEqual(["reset", "api", "list"]);
    expect(add).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith(API_PORT);
    expect(calls.reset).toBe(1);
    expect(calls.finish).toBe(0);
    const next = state();
    expect(next.gatewayWorkspaces).toBe(authoritativeList.items);
    expect(next.gatewayWorkspacesStale).toBe(false);
    expect(next.activeGatewayWorkspaceId).toBe("ws-current");
    expect(next.gatewayError).toBe("连接被拒绝");
    expect(next.error).toBeNull();
    expect(next.status).toBe("连接远程 Gateway 失败: 连接被拒绝");
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
  });

  test("注册失败且权威列表回读失败时保留旧列表、标记失效并展示两个错误", async () => {
    const failure = new Error("连接被拒绝");
    const add = spyOnGatewayApi("addSshGatewayWorkspace").mockRejectedValue(failure);
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-old" })];
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(
      new Error("列表不可用"),
    );
    const { hook, state, calls } = await mountHook(appState({
      gatewayWorkspaces: previousWorkspaces,
      gatewayWorkspacesStale: false,
    }));

    await expect(hook.addSshGatewayWorkspace({
      remote_gateway_port: 8014,
      ssh_config_host: "remote-host",
    })).rejects.toBe(failure);

    expect(add).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    const message = "连接被拒绝；读取 Gateway 工作区列表失败: 列表不可用";
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().gatewayError).toBe(message);
    expect(state().status).toBe(`连接远程 Gateway 失败: ${message}`);
    expect(calls.finish).toBe(0);
  });

  test("注册失败后的列表回读遇到新请求代次时不覆盖新列表", async () => {
    const failure = new Error("连接被拒绝");
    const add = spyOnGatewayApi("addSshGatewayWorkspace").mockRejectedValue(failure);
    let resolveList!: (value: GatewayWorkspaceList) => void;
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(
      () => new Promise((resolve) => { resolveList = resolve; }),
    );
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-old" })];
    const { hook, state, gatewayListRequestRef } = await mountHook(appState({
      gatewayWorkspaces: previousWorkspaces,
      gatewayWorkspacesStale: false,
    }));
    const pending = hook.addSshGatewayWorkspace({
      remote_gateway_port: 8014,
      ssh_config_host: "remote-host",
    }).catch((error: unknown) => error);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(add).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);

    gatewayListRequestRef.current += 1;
    await act(async () => {
      resolveList(workspaceList("ws-late", [gatewayWorkspace({ workspace_id: "ws-late" })]));
      await Promise.resolve();
    });

    expect(await pending).toBe(failure);
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().activeGatewayWorkspaceId).toBe("ws-active");
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().gatewayError).toBeNull();
  });

  test("注册失败后的列表回读遇到新选择意图时不覆盖活动工作区", async () => {
    const failure = new Error("连接被拒绝");
    const add = spyOnGatewayApi("addSshGatewayWorkspace").mockRejectedValue(failure);
    let resolveList!: (value: GatewayWorkspaceList) => void;
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(
      () => new Promise((resolve) => { resolveList = resolve; }),
    );
    const previousWorkspaces = [gatewayWorkspace({ workspace_id: "ws-old" })];
    const { hook, state, selectionIntentRef } = await mountHook(appState({
      gatewayWorkspaces: previousWorkspaces,
      gatewayWorkspacesStale: false,
    }));
    const pending = hook.addSshGatewayWorkspace({
      remote_gateway_port: 8014,
      ssh_config_host: "remote-host",
    }).catch((error: unknown) => error);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(add).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);

    selectionIntentRef.current += 1;
    await act(async () => {
      resolveList(workspaceList("ws-late", [gatewayWorkspace({ workspace_id: "ws-late" })]));
      await Promise.resolve();
    });

    expect(await pending).toBe(failure);
    expect(state().gatewayWorkspaces).toBe(previousWorkspaces);
    expect(state().activeGatewayWorkspaceId).toBe("ws-active");
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().gatewayError).toBeNull();
  });

  test("远程 Gateway 已连接但界面同步失败时写入复合文案", async () => {
    spyOnGatewayApi("addSshGatewayWorkspace").mockResolvedValue(workspaceList(null, []));
    const { hook, state, calls } = await mountHook(appState(), {
      finishWorkspaceRefresh: async () => {
        throw new Error("刷新失败");
      },
    });

    const message = "远程 Gateway 已连接，但界面同步失败: 刷新失败";
    await expect(hook.addSshGatewayWorkspace({
      remote_gateway_port: 8014,
      ssh_config_host: "remote-host",
    })).rejects.toThrow(message);

    expect(calls.reset).toBe(1);
    expect(calls.finish).toBe(1);
    const next = state();
    expect(next.gatewayError).toBe(message);
    expect(next.error).toBeNull();
    expect(next.status).toBe(message);
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
  });
});

test("apiPort 为空时使用默认工作区后端端口", async () => {
  const reorder = spyOnGatewayApi("reorderGatewayWorkspaces").mockResolvedValue(
    workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-active" })]),
  );
  let hook: ReturnType<typeof useGatewayWorkspaceMutations> | undefined;
  let current = appState();
  const selectionIntentRef = { current: 0 };
  const gatewayListRequestRef = { current: 0 };
  const queue = createSerialTaskQueue();

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceMutations({
      selectionIntentRef,
      gatewayListRequestRef,
      runGatewayWorkspaceListTask: queue.enqueue,
      apiPort: null,
      activeGatewayWorkspaceId: "ws-active",
      setState: (update) => {
        current = typeof update === "function" ? update(current) : update;
      },
      abortCurrentStream: () => undefined,
      invalidateWorkspaceRefreshes: () => undefined,
      finishWorkspaceRefresh: async () => "ws-refreshed",
      resetWorkspaceScopedState: () => undefined,
      updateUiSettings: async () => undefined,
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);

  await hook!.reorderGatewayWorkspaces(["ws-active"]);

  expect(reorder).toHaveBeenCalledWith(DEFAULT_BACKEND_PORT, { workspace_ids: ["ws-active"] });
});


test("受控删除对整个已确认组暂停观察，409后完整对账并释放暂停", async () => {
  const failure = new HttpRequestError(409, "Job 阻塞", { status: "blocked", blockers: [] }, "/api/gateway/workspaces/ws-other");
  const group = { expected_backend_generation: "generation-1", expected_workspace_ids: ["ws-other", "ws-sibling"], desired_workspace_ids: ["ws-sibling"] };
  const controllers = [new AbortController(), new AbortController()];
  const releases = controllers.map((controller, index) => registerWorkspaceObservation(group.expected_workspace_ids[index]!, controller));
  const remove = spyOnGatewayApi("removeGatewayWorkspace").mockImplementation(async () => {
    expect(controllers.every((controller) => controller.signal.aborted)).toBe(true);
    throw failure;
  });
  spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(workspaceList("ws-active", [
    gatewayWorkspace({ workspace_id: "ws-other", backend_generation: "generation-1" }),
    gatewayWorkspace({ workspace_id: "ws-sibling", backend_generation: "generation-1" }),
  ]));
  const { hook, state } = await mountHook(appState());
  await expect(hook.removeGatewayWorkspace("ws-other", group, true)).rejects.toBe(failure);
  expect(remove).toHaveBeenCalledWith(API_PORT, "ws-other", group, true);
  expect(state().gatewayWorkspaces.map((item) => item.workspace_id)).toEqual(["ws-other", "ws-sibling"]);
  for (const identity of group.expected_workspace_ids) {
    const next = new AbortController();
    const release = registerWorkspaceObservation(identity, next);
    expect(next.signal.aborted).toBe(false);
    release();
  }
  for (const release of releases) release();
});
