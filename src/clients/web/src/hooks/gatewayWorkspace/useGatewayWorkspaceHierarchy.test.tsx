import { afterEach, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as gatewayApi from "../../gatewayApi";
import type { GatewayWorkspace, GatewayWorkspaceList } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { useGatewayWorkspaceHierarchy } from "./useGatewayWorkspaceHierarchy";

const API_PORT = 49_631;

function gatewayWorkspace(
  overrides: Partial<GatewayWorkspace> = {},
): GatewayWorkspace {
  return {
    workspace_id: "ws-1",
    parent_workspace_id: null,
    name: "工作区一",
    root_path: "/tmp/workspace-one",
    backend_url: "http://127.0.0.1:41000",
    connection_kind: "local",
    status: "ready",
    active: false,
    managed: true,
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
    workspaceRoot: "/tmp/stale-root",
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

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceHierarchy(API_PORT, (update) => {
      current = typeof update === "function" ? update(current) : update;
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);
  return { hook: hook!, state: () => current };
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
  // workspaceRoot/workspaceName，文件树与预览就会按新工作区 id 去读旧根路径。
  spyOnGatewayApi("updateGatewayWorkspace").mockRejectedValue(new Error("父工作区不存在"));
  const list = spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
    workspaceList("ws-other", [
      gatewayWorkspace({
        workspace_id: "ws-other",
        name: "对账后的活动工作区",
        root_path: "/tmp/reconciled-active",
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
  expect(next.workspaceRoot).toBe("/tmp/reconciled-active");
  expect(next.workspaceName).toBe("对账后的活动工作区");
  expect(next.gatewayError).toBe("父工作区不存在");
  expect(next.status).toBe("更新工作区父子关系失败: 父工作区不存在");
});

test("成功时整体替换工作区列表与活动工作区派生字段", async () => {
  spyOnGatewayApi("updateGatewayWorkspace").mockResolvedValue(
    workspaceList("ws-1", [
      gatewayWorkspace({
        workspace_id: "ws-1",
        name: "工作区一",
        root_path: "/tmp/one",
        parent_workspace_id: "ws-parent",
      }),
    ]),
  );
  const { hook, state } = await mountHook(appState());

  await hook("ws-1", "ws-parent");

  const next = state();
  expect(next.activeGatewayWorkspaceId).toBe("ws-1");
  expect(next.workspaceRoot).toBe("/tmp/one");
  expect(next.workspaceName).toBe("工作区一");
  expect(next.gatewayError).toBeNull();
  expect(next.error).toBeNull();
  expect(next.status).toBe("工作区「工作区一」已移入父工作区");
});
