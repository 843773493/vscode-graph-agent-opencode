import { afterEach, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as gatewayApi from "../../gatewayApi";
import type { GatewayWorkspace, GatewayWorkspaceList } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { useGatewayWorkspaceHierarchy } from "./useGatewayWorkspaceHierarchy";
import { createSerialTaskQueue } from "../runtime/serialTaskQueue";

const API_PORT = 49_631;

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

function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    gatewayWorkspaces: [],
    activeGatewayWorkspaceId: "ws-active",
    workspaceName: "陈旧的工作区",
    gatewayError: null,
    error: null,
    status: "",
    ...overrides,
  } as unknown as AppState;
}

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];

async function mountHook(initialState: AppState) {
  let current = initialState;
  let hook: ReturnType<typeof useGatewayWorkspaceHierarchy> | undefined;
  const gatewayListRequestRef = { current: 0 };
  const selectionIntentRef = { current: 0 };
  const queue = createSerialTaskQueue();
  const setState = (update: AppState | ((previous: AppState) => AppState)) => {
    current = typeof update === "function" ? update(current) : update;
  };

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceHierarchy(API_PORT, setState, gatewayListRequestRef, queue.enqueue, selectionIntentRef);
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);
  return { hook: hook!, state: () => current, setState, selectionIntentRef };
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

test("失败对账时工作区根路径与名称跟着活动工作区一起替换", async () => {
  // 对账把 activeGatewayWorkspaceId 换成回滚结果里的活动工作区后，若还留着旧的
  // workspaceName，文件树与预览就会按新工作区 id 去读旧根路径。
  spyOnGatewayApi("updateGatewayWorkspace").mockRejectedValue(new Error("父工作区不存在"));
  const list = spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
    workspaceList("ws-other", [
      gatewayWorkspace({
        workspace_id: "ws-other",
        name: "对账后的活动工作区",
        }),
    ]),
  );
  const { hook, state } = await mountHook(appState({
    workspaceSwitching: true,
  } as Partial<AppState>));

  await expect(hook("ws-1", "ws-missing")).rejects.toThrow(
    "父工作区不存在",
  );

  expect(list).toHaveBeenCalledWith(API_PORT);
  const next = state();
  expect(next.activeGatewayWorkspaceId).toBe("ws-other");
  expect(next.workspaceName).toBe("对账后的活动工作区");
  expect(next.gatewayError).toBe("父工作区不存在");
  expect(next.status).toBe("更新工作区父子关系失败: 父工作区不存在");
});

test("父子关系更新与列表对账都失败时保留原始错误并标记工作区列表过期", async () => {
  const operationError = new Error("父工作区不存在");
  const reconciliationError = new Error("Gateway 列表不可用");
  const update = spyOnGatewayApi("updateGatewayWorkspace").mockRejectedValue(operationError);
  const list = spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(reconciliationError);
  const { hook, state } = await mountHook(appState({ gatewayWorkspacesStale: false }));

  await expect(hook("ws-1", "ws-missing")).rejects.toBe(operationError);

  expect(update).toHaveBeenCalledTimes(1);
  expect(list).toHaveBeenCalledTimes(1);
  const next = state();
  expect(next.gatewayWorkspacesStale).toBe(true);
  expect(next.gatewayError).toBe("父工作区不存在；重新读取工作区列表也失败: Gateway 列表不可用");
  expect(next.status).toBe("更新工作区父子关系失败: 父工作区不存在；重新读取工作区列表也失败: Gateway 列表不可用");
});

test("列表对账成功期间选择意图改变时仍替换完整列表并保留较新的选择状态", async () => {
  const operationError = new Error("父工作区不存在");
  const update = spyOnGatewayApi("updateGatewayWorkspace").mockRejectedValue(operationError);
  let resolveRead!: (list: GatewayWorkspaceList) => void;
  let markReadStarted!: () => void;
  const readStarted = new Promise<void>((resolve) => { markReadStarted = resolve; });
  const readResult = new Promise<GatewayWorkspaceList>((resolve) => { resolveRead = resolve; });
  const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => {
    markReadStarted();
    return readResult;
  });
  const reconciledList = workspaceList("ws-reconciled", [
    gatewayWorkspace({
      workspace_id: "ws-reconciled",
      name: "对账后的工作区",
      }),
    gatewayWorkspace({
      workspace_id: "ws-sibling",
      name: "对账后的其他工作区",
      }),
  ]);
  const { hook, state, setState, selectionIntentRef } = await mountHook(appState({
    gatewayWorkspaces: [gatewayWorkspace({ workspace_id: "ws-obsolete", name: "旧工作区" })],
    gatewayWorkspacesStale: false,
  }));

  const operation = hook("ws-1", "ws-missing");
  await readStarted;
  act(() => {
    selectionIntentRef.current += 1;
    setState((previous) => ({
      ...previous,
      activeGatewayWorkspaceId: "ws-new-selection",
      workspaceName: "较新的选择",
      gatewayError: "较新的错误状态",
      status: "用户已切换到较新工作区",
    }));
  });

  await act(async () => {
    resolveRead(reconciledList);
    await expect(operation).rejects.toBe(operationError);
  });

  expect(update).toHaveBeenCalledTimes(1);
  expect(list).toHaveBeenCalledTimes(1);
  const next = state();
  expect(next.gatewayWorkspaces).toEqual(reconciledList.items);
  expect(next.gatewayWorkspaces.map((workspace) => workspace.workspace_id)).toEqual([
    "ws-reconciled",
    "ws-sibling",
  ]);
  expect(next.activeGatewayWorkspaceId).toBe("ws-new-selection");
  expect(next.workspaceName).toBe("较新的选择");
  expect(next.gatewayError).toBe("较新的错误状态");
  expect(next.status).toBe("用户已切换到较新工作区");
  expect(next.gatewayWorkspacesStale).toBe(false);
});

test("列表对账失败期间选择意图改变时仍标记列表过期并保留较新的选择状态", async () => {
  const operationError = new Error("父工作区不存在");
  const reconciliationError = new Error("Gateway 列表不可用");
  const update = spyOnGatewayApi("updateGatewayWorkspace").mockRejectedValue(operationError);
  let rejectRead!: (error: Error) => void;
  let markReadStarted!: () => void;
  const readStarted = new Promise<void>((resolve) => { markReadStarted = resolve; });
  const readResult = new Promise<GatewayWorkspaceList>((_resolve, reject) => { rejectRead = reject; });
  const list = spyOnGatewayApi("listGatewayWorkspaces").mockImplementation(() => {
    markReadStarted();
    return readResult;
  });
  const { hook, state, setState, selectionIntentRef } = await mountHook(appState({
    gatewayWorkspacesStale: false,
  }));

  const operation = hook("ws-1", "ws-missing");
  await readStarted;
  act(() => {
    selectionIntentRef.current += 1;
    setState((previous) => ({
      ...previous,
      activeGatewayWorkspaceId: "ws-new-selection",
      workspaceName: "较新的选择",
      gatewayError: "较新的错误状态",
      status: "用户已切换到较新工作区",
    }));
  });

  await act(async () => {
    rejectRead(reconciliationError);
    await expect(operation).rejects.toBe(operationError);
  });

  expect(update).toHaveBeenCalledTimes(1);
  expect(list).toHaveBeenCalledTimes(1);
  const next = state();
  expect(next.gatewayWorkspacesStale).toBe(true);
  expect(next.activeGatewayWorkspaceId).toBe("ws-new-selection");
  expect(next.workspaceName).toBe("较新的选择");
  expect(next.gatewayError).toBe("较新的错误状态");
  expect(next.status).toBe("用户已切换到较新工作区");
});

test("成功时整体替换工作区列表与活动工作区派生字段", async () => {
  const updatedList = workspaceList("ws-1", [
    gatewayWorkspace({
      workspace_id: "ws-1",
      name: "工作区一",
      parent_workspace_id: "ws-parent",
    }),
    gatewayWorkspace({
      workspace_id: "ws-2",
      name: "工作区二",
      }),
  ]);
  spyOnGatewayApi("updateGatewayWorkspace").mockResolvedValue(
    updatedList,
  );
  const { hook, state } = await mountHook(appState({
    gatewayWorkspaces: [gatewayWorkspace({ workspace_id: "ws-obsolete", name: "旧工作区" })],
  }));

  await hook("ws-1", "ws-parent");

  const next = state();
  expect(next.gatewayWorkspaces).toEqual(updatedList.items);
  expect(next.gatewayWorkspaces.map((workspace) => workspace.workspace_id)).toEqual([
    "ws-1",
    "ws-2",
  ]);
  expect(next.activeGatewayWorkspaceId).toBe("ws-1");
  expect(next.workspaceName).toBe("工作区一");
  expect(next.gatewayError).toBeNull();
  expect(next.error).toBeNull();
  expect(next.status).toBe("工作区「工作区一」已移入父工作区");
});
