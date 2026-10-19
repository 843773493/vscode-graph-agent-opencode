import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as gatewayApi from "../../gatewayApi";
import type {
  GatewayRuntimeBlocker,
  GatewayRuntimeGroupRequest,
  GatewayRuntimeRestartResult,
  GatewayRuntimeStateResult,
  GatewayWorkspace,
  GatewayWorkspaceList,
} from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { registerWorkspaceObservation } from "../workspace/workspaceObservationLifecycle";
import { useGatewayWorkspaceRuntimeLifecycle } from "./useGatewayWorkspaceRuntimeLifecycle";
import { createSerialTaskQueue } from "../runtime/serialTaskQueue";

const API_PORT = 49_630;
const WORKSPACE_ID = "ws-action";
const SIBLING_WORKSPACE_ID = "ws-sibling";
const GROUP_REQUEST: GatewayRuntimeGroupRequest = {
  expected_backend_generation: "generation-1",
  expected_workspace_ids: [WORKSPACE_ID, SIBLING_WORKSPACE_ID],
  desired_workspace_ids: [WORKSPACE_ID, SIBLING_WORKSPACE_ID],
};

function gatewayWorkspace(
  overrides: Partial<GatewayWorkspace> = {},
): GatewayWorkspace {
  return {
    workspace_id: "ws-old",
    parent_workspace_id: null,
    name: "测试工作区",
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
    checked_at: "2026-10-14T00:00:00Z",
    ...overrides,
  };
}

function workspaceList(
  activeWorkspaceId: string | null,
  items: GatewayWorkspace[],
): GatewayWorkspaceList {
  return { active_workspace_id: activeWorkspaceId, items };
}

function responseWorkspaces(): GatewayWorkspaceList {
  return workspaceList("ws-next", [
    gatewayWorkspace({ workspace_id: WORKSPACE_ID, name: "操作后的工作区" }),
    gatewayWorkspace({ workspace_id: SIBLING_WORKSPACE_ID, name: "完整组成员" }),
    gatewayWorkspace({ workspace_id: "ws-next", name: "刷新后的活动工作区" }),
  ]);
}

function blocker(
  workspaceId: string,
  kind: GatewayRuntimeBlocker["kind"],
  resourceId: string,
): GatewayRuntimeBlocker {
  return {
    workspace_id: workspaceId,
    kind,
    resource_id: resourceId,
    session_id: "session-running",
    status: "running",
    detail: "仍在运行",
  };
}

function restartResult(
  forced: boolean,
  status: GatewayRuntimeRestartResult["status"] = "restarted",
  blockers: GatewayRuntimeBlocker[] = [],
): GatewayRuntimeRestartResult {
  return {
    workspace_id: WORKSPACE_ID,
    backend_generation: "generation-2",
    affected_workspace_ids: [WORKSPACE_ID, SIBLING_WORKSPACE_ID],
    status,
    forced,
    blockers,
    workspaces: responseWorkspaces(),
  };
}

function stateResult(
  status: GatewayRuntimeStateResult["status"] = "stopped",
  blockers: GatewayRuntimeBlocker[] = [],
): GatewayRuntimeStateResult {
  return {
    workspace_id: WORKSPACE_ID,
    backend_generation: "generation-2",
    affected_workspace_ids: [WORKSPACE_ID, SIBLING_WORKSPACE_ID],
    status,
    blockers,
    workspaces: responseWorkspaces(),
  };
}

function appState(): AppState {
  return {
    activeGatewayWorkspaceId: "ws-old",
    gatewayWorkspaces: [gatewayWorkspace()],
    gatewayWorkspacesStale: true,
    workspaceSwitching: false,
    gatewayError: null,
    error: null,
    status: "",
  } as unknown as AppState;
}

interface MountOptions {
  finishWorkspaceRefresh?: (refs: {
    selectionIntentRef: { current: number };
    gatewayListRequestRef: { current: number };
  }) => Promise<string | null>;
  finishUsesQueue?: boolean;
  refreshGatewayState?: () => Promise<void>;
}

interface MountedHook {
  hook: ReturnType<typeof useGatewayWorkspaceRuntimeLifecycle>;
  state: () => AppState;
  updateState: (update: React.SetStateAction<AppState>) => void;
  deferNextStateUpdater: (afterEnqueue: () => void) => void;
  flushDeferredStateUpdaters: () => void;
  deferredStateUpdaterCount: () => number;
  calls: { finish: number; refresh: number };
  refs: {
    selectionIntentRef: { current: number };
    gatewayListRequestRef: { current: number };
  };
  queue: ReturnType<typeof createSerialTaskQueue>;
}

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];
const releaseObservations: Array<() => void> = [];

async function mountHook(options: MountOptions = {}): Promise<MountedHook> {
  let current = appState();
  let hook: ReturnType<typeof useGatewayWorkspaceRuntimeLifecycle> | undefined;
  const calls = { finish: 0, refresh: 0 };
  const selectionIntentRef = { current: 0 };
  const gatewayListRequestRef = { current: 0 };
  const queue = createSerialTaskQueue();
  const deferredStateUpdaters: Array<React.SetStateAction<AppState>> = [];
  let afterNextUpdaterEnqueue: (() => void) | null = null;
  const updateState = (update: React.SetStateAction<AppState>) => {
    if (afterNextUpdaterEnqueue && typeof update === "function") {
      const afterEnqueue = afterNextUpdaterEnqueue;
      afterNextUpdaterEnqueue = null;
      deferredStateUpdaters.push(update);
      afterEnqueue();
      return;
    }
    current = typeof update === "function" ? update(current) : update;
  };
  const deferNextStateUpdater = (afterEnqueue: () => void) => {
    if (afterNextUpdaterEnqueue) throw new Error("已有待延后执行的 state updater");
    afterNextUpdaterEnqueue = afterEnqueue;
  };
  const flushDeferredStateUpdaters = () => {
    for (const update of deferredStateUpdaters.splice(0)) {
      current = typeof update === "function" ? update(current) : update;
    }
  };

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceRuntimeLifecycle({
      selectionIntentRef,
      gatewayListRequestRef,
      runGatewayWorkspaceListTask: queue.enqueue,
      apiPort: API_PORT,
      currentSessionId: "session-current",
      setState: updateState,
      finishWorkspaceRefresh: async (preferredSessionId) => {
        calls.finish += 1;
        if (preferredSessionId !== "session-current") {
          throw new Error(`刷新使用了错误的会话: ${preferredSessionId}`);
        }
        if (options.finishUsesQueue) {
          await queue.enqueue(async () => undefined);
        }
        if (options.finishWorkspaceRefresh) {
          return await options.finishWorkspaceRefresh({ selectionIntentRef, gatewayListRequestRef });
        }
        return null;
      },
      refreshGatewayState: async () => {
        calls.refresh += 1;
        await options.refreshGatewayState?.();
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
    updateState,
    deferNextStateUpdater,
    flushDeferredStateUpdaters,
    deferredStateUpdaterCount: () => deferredStateUpdaters.length,
    calls,
    refs: { selectionIntentRef, gatewayListRequestRef },
    queue,
  };
}

function spyOnGatewayApi<Name extends keyof typeof gatewayApi>(
  name: Name,
): ReturnType<typeof spyOn<typeof gatewayApi, Name>> {
  const spy = spyOn(gatewayApi, name);
  restoreSpies.push(() => spy.mockRestore());
  return spy;
}

type RuntimeAction = "safe-restart" | "force-restart" | "stop";

function mockActionResult(
  action: RuntimeAction,
  result: GatewayRuntimeRestartResult | GatewayRuntimeStateResult,
): void {
  switch (action) {
    case "safe-restart":
      spyOnGatewayApi("safeRestartManagedGatewayWorkspaceBackend").mockResolvedValue(
        result as GatewayRuntimeRestartResult,
      );
      return;
    case "force-restart":
      spyOnGatewayApi("forceRestartManagedGatewayWorkspaceBackend").mockResolvedValue(
        result as GatewayRuntimeRestartResult,
      );
      return;
    case "stop":
      spyOnGatewayApi("stopManagedGatewayWorkspaceBackend").mockResolvedValue(
        result as GatewayRuntimeStateResult,
      );
  }
}

function mockActionFailure(action: RuntimeAction, failure: Error): void {
  switch (action) {
    case "safe-restart":
      spyOnGatewayApi("safeRestartManagedGatewayWorkspaceBackend").mockRejectedValue(failure);
      return;
    case "force-restart":
      spyOnGatewayApi("forceRestartManagedGatewayWorkspaceBackend").mockRejectedValue(failure);
      return;
    case "stop":
      spyOnGatewayApi("stopManagedGatewayWorkspaceBackend").mockRejectedValue(failure);
  }
}

function invokeAction(
  action: RuntimeAction,
  hook: ReturnType<typeof useGatewayWorkspaceRuntimeLifecycle>,
): Promise<unknown> {
  switch (action) {
    case "safe-restart":
      return hook.safeRestartManagedGatewayWorkspaceBackend(WORKSPACE_ID, GROUP_REQUEST);
    case "force-restart":
      return hook.forceRestartManagedGatewayWorkspaceBackend(WORKSPACE_ID, GROUP_REQUEST);
    case "stop":
      return hook.stopManagedGatewayWorkspaceBackend(WORKSPACE_ID, GROUP_REQUEST);
  }
}

function registerObservations(): AbortController[] {
  const controllers = [new AbortController(), new AbortController()];
  [WORKSPACE_ID, SIBLING_WORKSPACE_ID].forEach((workspaceId, index) => {
    releaseObservations.push(
      registerWorkspaceObservation(workspaceId, controllers[index]!),
    );
  });
  return controllers;
}

function expectObservationsResumed(): void {
  for (const workspaceId of [WORKSPACE_ID, SIBLING_WORKSPACE_ID]) {
    const controller = new AbortController();
    releaseObservations.push(registerWorkspaceObservation(workspaceId, controller));
    expect(controller.signal.aborted).toBe(false);
  }
}

afterEach(() => {
  for (const release of releaseObservations.splice(0)) release();
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restoreSpies.splice(0)) restore();
});

function deferred<Value>() {
  let resolve!: (value: Value) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<Value>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("Gateway 工作区 reconnect 权威收敛", () => {
  test("POST 拒绝后在同一队列读取一次完整权威列表，并在队列外完成活动工作区收尾", async () => {
    const postError = new Error("Gateway 在提交后返回 502");
    const authority = responseWorkspaces();
    const pendingList = deferred<GatewayWorkspaceList>();
    spyOnGatewayApi("reconnectGatewayWorkspace").mockRejectedValue(postError);
    const listSpy = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => pendingList.promise);
    const { hook, state, calls, queue } = await mountHook({
      finishUsesQueue: true,
      finishWorkspaceRefresh: async () => authority.active_workspace_id,
    });
    let competingTaskStarted = false;
    const reconnect = hook.reconnectGatewayWorkspace(WORKSPACE_ID);
    for (let attempt = 0; attempt < 8 && listSpy.mock.calls.length === 0; attempt += 1) {
      await Promise.resolve();
    }
    expect(listSpy).toHaveBeenCalledTimes(1);
    const competingTask = queue.enqueue(async () => {
      competingTaskStarted = true;
    });
    for (let turn = 0; turn < 4; turn += 1) await Promise.resolve();
    const competingTaskWasBlocked = !competingTaskStarted;
    pendingList.resolve(authority);

    await expect(reconnect).rejects.toBe(postError);
    await competingTask;

    expect(listSpy).toHaveBeenCalledTimes(1);
    expect(competingTaskWasBlocked).toBe(true);
    expect(competingTaskStarted).toBe(true);
    expect(state().gatewayWorkspaces).toBe(authority.items);
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().activeGatewayWorkspaceId).toBe(authority.active_workspace_id);
    expect(calls.finish).toBe(1);
    expect(state().gatewayError).toBe(postError.message);
    expect(state().status).toContain("已读取最新工作区状态");
  });

  test("authority GET 失败时标记 stale、显示双错，并仍传播原 POST Error", async () => {
    const postError = new Error("POST 502");
    const readbackError = new Error("Gateway 列表读取超时");
    spyOnGatewayApi("reconnectGatewayWorkspace").mockRejectedValue(postError);
    const listSpy = spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(readbackError);
    const { hook, state, calls } = await mountHook();

    await expect(hook.reconnectGatewayWorkspace(WORKSPACE_ID)).rejects.toBe(postError);

    expect(listSpy).toHaveBeenCalledTimes(1);
    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().gatewayError).toContain(postError.message);
    expect(state().gatewayError).toContain(readbackError.message);
    expect(state().status).toContain(readbackError.message);
    expect(calls.finish).toBe(0);
  });

  test("selection 已变化但 generation 仍当前时标记列表 stale，并保留新 owner 错误", async () => {
    const postError = new Error("POST 502");
    const readbackError = new Error("Gateway 列表读取超时");
    const pendingList = deferred<GatewayWorkspaceList>();
    spyOnGatewayApi("reconnectGatewayWorkspace").mockRejectedValue(postError);
    const listSpy = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => pendingList.promise);
    const { hook, state, updateState, refs } = await mountHook();

    const reconnect = hook.reconnectGatewayWorkspace(WORKSPACE_ID);
    for (let attempt = 0; attempt < 8 && listSpy.mock.calls.length === 0; attempt += 1) {
      await Promise.resolve();
    }
    expect(listSpy).toHaveBeenCalledTimes(1);
    updateState((prev) => ({
      ...prev,
      gatewayWorkspacesStale: false,
      gatewayError: "新 owner 错误",
      status: "新 owner 状态",
    }));
    refs.selectionIntentRef.current += 1;
    pendingList.reject(readbackError);

    await expect(reconnect).rejects.toBe(postError);

    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().gatewayError).toBe("新 owner 错误");
    expect(state().status).toContain("新 owner 状态");
    expect(state().status).toContain(readbackError.message);
  });

  for (const supersession of ["generation", "selection"] as const) {
    test(`迟到的 ${supersession} 结果不会覆盖新 owner`, async () => {
      const postError = new Error("POST 502");
      const authority = responseWorkspaces();
      const pendingList = deferred<GatewayWorkspaceList>();
      spyOnGatewayApi("reconnectGatewayWorkspace").mockRejectedValue(postError);
      const listSpy = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => pendingList.promise);
      const { hook, state, updateState, refs, calls } = await mountHook();
      const newerItems = [gatewayWorkspace({ workspace_id: "ws-owner", name: "新 owner" })];

      const reconnect = hook.reconnectGatewayWorkspace(WORKSPACE_ID);
      for (let attempt = 0; attempt < 8 && listSpy.mock.calls.length === 0; attempt += 1) {
        await Promise.resolve();
      }
      expect(listSpy).toHaveBeenCalledTimes(1);
      updateState((prev) => ({
        ...prev,
        gatewayWorkspaces: newerItems,
        activeGatewayWorkspaceId: "ws-owner",
        gatewayError: "新 owner 错误",
        status: "新 owner 状态",
      }));
      if (supersession === "generation") refs.gatewayListRequestRef.current += 1;
      else refs.selectionIntentRef.current += 1;
      pendingList.resolve(authority);

      await expect(reconnect).rejects.toBe(postError);

      expect(state().gatewayWorkspaces).toBe(
        supersession === "generation" ? newerItems : authority.items,
      );
      expect(state().activeGatewayWorkspaceId).toBe("ws-owner");
      expect(state().gatewayWorkspacesStale).toBe(supersession === "generation");
      expect(state().gatewayError).toBe("新 owner 错误");
      expect(state().status).toContain("新 owner 状态");
      expect(calls.finish).toBe(0);
    });
  }

  for (const supersession of ["generation", "selection"] as const) {
    test(`${supersession} 在 state updater 排队后变化时，由 updater 动态归属决定写入`, async () => {
      const postError = new Error("POST 502");
      const authority = responseWorkspaces();
      const pendingList = deferred<GatewayWorkspaceList>();
      spyOnGatewayApi("reconnectGatewayWorkspace").mockRejectedValue(postError);
      const listSpy = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => pendingList.promise);
      const {
        hook,
        state,
        updateState,
        deferNextStateUpdater,
        flushDeferredStateUpdaters,
        deferredStateUpdaterCount,
        refs,
        calls,
      } = await mountHook();

      const reconnect = hook.reconnectGatewayWorkspace(WORKSPACE_ID);
      for (let attempt = 0; attempt < 8 && listSpy.mock.calls.length === 0; attempt += 1) {
        await Promise.resolve();
      }
      expect(listSpy).toHaveBeenCalledTimes(1);
      const newerItems = [gatewayWorkspace({ workspace_id: "ws-owner", name: "新 owner" })];
      deferNextStateUpdater(() => {
        if (supersession === "generation") refs.gatewayListRequestRef.current += 1;
        else refs.selectionIntentRef.current += 1;
        updateState((prev) => ({
          ...prev,
          gatewayWorkspaces: newerItems,
          gatewayWorkspacesStale: false,
          activeGatewayWorkspaceId: "ws-owner",
          gatewayError: "新 owner 错误",
          status: "新 owner 状态",
        }));
      });
      pendingList.resolve(authority);

      await expect(reconnect).rejects.toBe(postError);

      expect(deferredStateUpdaterCount()).toBe(1);
      expect(state().gatewayWorkspaces).toBe(newerItems);
      expect(state().activeGatewayWorkspaceId).toBe("ws-owner");
      expect(calls.finish).toBe(0);
      flushDeferredStateUpdaters();
      expect(state().gatewayWorkspaces).toBe(
        supersession === "generation" ? newerItems : authority.items,
      );
      expect(state().activeGatewayWorkspaceId).toBe("ws-owner");
      expect(state().gatewayWorkspacesStale).toBe(false);
      expect(state().gatewayError).toBe("新 owner 错误");
      expect(state().status).toContain("新 owner 状态");
    });
  }

  test("POST 成功但 finisher 失败时保留已消费 DTO 并明确报告同步失败", async () => {
    const result = responseWorkspaces();
    const refreshError = new Error("session bootstrap 失败");
    spyOnGatewayApi("reconnectGatewayWorkspace").mockResolvedValue(result);
    const listSpy = spyOnGatewayApi("listGatewayWorkspaces");
    const { hook, state, calls } = await mountHook({
      finishWorkspaceRefresh: async () => {
        throw refreshError;
      },
    });

    await expect(hook.reconnectGatewayWorkspace(WORKSPACE_ID)).rejects.toThrow("已重新连接，但界面同步失败");

    expect(listSpy).not.toHaveBeenCalled();
    expect(state().gatewayWorkspaces).toBe(result.items);
    expect(state().gatewayWorkspacesStale).toBe(false);
    expect(state().activeGatewayWorkspaceId).toBe(result.active_workspace_id);
    expect(state().gatewayError).toContain("已重新连接，但界面同步失败");
    expect(state().gatewayError).toContain(refreshError.message);
    expect(state().status).toBe(state().gatewayError ?? "");
    expect(calls.finish).toBe(1);
  });

  test("finisher 返回 null 时识别刷新作废，POST 原 Error 仍是调用方错误", async () => {
    const postError = new Error("POST 502");
    const authority = responseWorkspaces();
    spyOnGatewayApi("reconnectGatewayWorkspace").mockRejectedValue(postError);
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(authority);
    const { hook, state, calls } = await mountHook({
      finishWorkspaceRefresh: async () => null,
    });

    await expect(hook.reconnectGatewayWorkspace(WORKSPACE_ID)).rejects.toBe(postError);

    expect(calls.finish).toBe(1);
    expect(state().gatewayError).toContain("工作区刷新未生效");
    expect(state().status).toContain(postError.message);
  });

  test("finisher 等待期间 owner 改变时不接受迟到刷新结果，并保留原 POST Error", async () => {
    const postError = new Error("POST 502");
    const authority = responseWorkspaces();
    spyOnGatewayApi("reconnectGatewayWorkspace").mockRejectedValue(postError);
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(authority);
    const { hook, state, calls } = await mountHook({
      finishWorkspaceRefresh: async ({ gatewayListRequestRef }) => {
        await Promise.resolve();
        gatewayListRequestRef.current += 1;
        return authority.active_workspace_id;
      },
    });

    await expect(hook.reconnectGatewayWorkspace(WORKSPACE_ID)).rejects.toBe(postError);

    expect(calls.finish).toBe(1);
    expect(state().gatewayWorkspaces).toBe(authority.items);
    expect(state().activeGatewayWorkspaceId).toBe(authority.active_workspace_id);
    expect(state().gatewayError).toBeNull();
    expect(state().status).toContain("工作区刷新结果已被更新的请求取代");
    expect(state().status).toContain(postError.message);
  });

  test("不接受列表中不存在的 active ID", async () => {
    const result = workspaceList("ws-missing", [gatewayWorkspace({ workspace_id: WORKSPACE_ID })]);
    spyOnGatewayApi("reconnectGatewayWorkspace").mockResolvedValue(result);
    const { hook, state, calls } = await mountHook();

    await expect(hook.reconnectGatewayWorkspace(WORKSPACE_ID)).rejects.toThrow("活动工作区不在工作区列表中");

    expect(state().gatewayWorkspaces).toBe(result.items);
    expect(state().gatewayWorkspacesStale).toBe(true);
    expect(state().activeGatewayWorkspaceId).toBe("ws-old");
    expect(state().gatewayError).toContain("活动工作区不在工作区列表中");
    expect(calls.finish).toBe(0);
  });
});

describe("Gateway 工作区运行时生命周期收尾", () => {
  for (const action of ["safe-restart", "force-restart", "stop"] as const) {
    test(`${action} 的后继刷新失败保留成功 DTO 并抛出刷新原错误`, async () => {
      const refreshFailure = new Error("界面刷新失败");
      const result = action === "stop"
        ? stateResult()
        : restartResult(action === "force-restart");
      mockActionResult(action, result);
      const { hook, state, calls } = await mountHook({
        finishWorkspaceRefresh: async () => {
          throw refreshFailure;
        },
      });
      const observations = registerObservations();

      await expect(invokeAction(action, hook)).rejects.toBe(refreshFailure);

      expect(observations.map((controller) => controller.signal.aborted)).toEqual([true, true]);
      expectObservationsResumed();
      expect(state().gatewayWorkspaces).toBe(result.workspaces.items);
      expect(state().activeGatewayWorkspaceId).toBe(result.workspaces.active_workspace_id);
      expect(state().gatewayWorkspacesStale).toBe(false);
      expect(state().gatewayError).toContain("刷新 Gateway 状态失败");
      expect(state().gatewayError).toContain(refreshFailure.message);
      expect(state().gatewayError).toBe(state().status);
      expect(calls.finish).toBe(1);
      expect(calls.refresh).toBe(1);
    });

    test(`${action} 的 API 真失败仍按操作失败重取状态并保留原错误`, async () => {
      const apiFailure = new Error("Gateway 操作被拒绝");
      mockActionFailure(action, apiFailure);
      const { hook, state, calls } = await mountHook();
      const originalWorkspaces = state().gatewayWorkspaces;
      const observations = registerObservations();

      await expect(invokeAction(action, hook)).rejects.toBe(apiFailure);

      expect(observations.map((controller) => controller.signal.aborted)).toEqual([true, true]);
      expectObservationsResumed();
      expect(state().gatewayWorkspaces).toBe(originalWorkspaces);
      expect(state().gatewayError).toBe(apiFailure.message);
      expect(state().status).toContain("失败: Gateway 操作被拒绝");
      expect(calls.finish).toBe(0);
      expect(calls.refresh).toBe(1);
    });
  }

  for (const action of ["safe-restart", "force-restart"] as const) {
    test(`${action} 返回 blocked 时仍保留阻塞状态和完整 DTO`, async () => {
      const blockers = [
        blocker(WORKSPACE_ID, "job", "job-1"),
        blocker(SIBLING_WORKSPACE_ID, "proxy_stream", "stream-2"),
      ];
      const result = restartResult(action === "force-restart", "blocked", blockers);
      mockActionResult(action, result);
      const { hook, state, calls } = await mountHook();
      const observations = registerObservations();

      await expect(invokeAction(action, hook)).resolves.toBe(result);

      expect(observations.map((controller) => controller.signal.aborted)).toEqual([true, true]);
      expectObservationsResumed();
      expect(state().gatewayWorkspaces).toBe(result.workspaces.items);
      expect(state().activeGatewayWorkspaceId).toBe(result.workspaces.active_workspace_id);
      expect(state().status).toContain("ws-action: 仍在运行");
      expect(state().status).toContain("ws-sibling: 仍在运行");
      expect(state().gatewayError).toBeNull();
      expect(calls.finish).toBe(1);
      expect(calls.refresh).toBe(0);
    });
  }

  test("stop 返回 blocked 时保留未关闭错误、完整 DTO 并释放观察暂停", async () => {
    const result = stateResult("blocked", [
      blocker(WORKSPACE_ID, "job", "job-1"),
    ]);
    mockActionResult("stop", result);
    const { hook, state, calls } = await mountHook();
    const observations = registerObservations();

    await expect(invokeAction("stop", hook)).rejects.toThrow("工作区仍有 1 个活动任务，未关闭");

    expect(observations.map((controller) => controller.signal.aborted)).toEqual([true, true]);
    expectObservationsResumed();
    expect(state().gatewayWorkspaces).toBe(result.workspaces.items);
    expect(state().activeGatewayWorkspaceId).toBe(result.workspaces.active_workspace_id);
    expect(state().gatewayError).toContain("未关闭");
    expect(state().status).toContain("关闭工作区失败: 工作区仍有");
    expect(calls.finish).toBe(0);
    expect(calls.refresh).toBe(1);
  });
});
