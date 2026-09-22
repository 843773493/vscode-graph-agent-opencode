import { afterEach, describe, expect, spyOn, test } from "bun:test";
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
import { useGatewayWorkspaceMutations } from "./useGatewayWorkspaceMutations";

const API_PORT = 49_611;

/** 覆盖 remove 链路里被读写的两个 UI 设置块，键名与 remove 测试用的工作区 ID 对齐。 */
const fakeUiSettings = {
  session_sidebar: {
    collapsed_workspace_ids: ["ws-active", "ws-other"],
    expanded_root_tree_ids: ["workspace:ws-active", "workspace:ws-other"],
  },
  workspace_file_tree: {
    expanded_paths_by_workspace: {
      "ws-active": ["/active"],
      "ws-other": ["/other"],
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

function session(sessionId: string): Session {
  return {
    session_id: sessionId,
    workspace_id: "ws_local",
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
    error: null,
    workspaceRoot: "/tmp/state-root",
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
  recentLocalWorkspacePaths?: string[];
  finishWorkspaceRefresh?: (preferredSessionId?: string | null) => Promise<string | null>;
  resetWorkspaceScopedState?: () => void;
  abortCurrentStream?: () => void;
  updateUiSettings?: (
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ) => Promise<void>;
}

interface MountedHook {
  hook: ReturnType<typeof useGatewayWorkspaceMutations>;
  state: () => AppState;
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

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceMutations({
      apiPort: API_PORT,
      activeGatewayWorkspaceId:
        options.activeGatewayWorkspaceId ?? initialState.activeGatewayWorkspaceId,
      recentLocalWorkspacePaths: options.recentLocalWorkspacePaths ?? [],
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
        if (!options.finishWorkspaceRefresh) return "ws-refreshed";
        return await options.finishWorkspaceRefresh(preferredSessionId);
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
  return { hook: hook!, state: () => current, calls };
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
  test("活动工作区为空时回退到上一个活动工作区并保留原工作区元数据", async () => {
    const reorder = spyOnGatewayApi("reorderGatewayWorkspaces").mockResolvedValue(
      workspaceList(null, [
        gatewayWorkspace({ workspace_id: "ws-1", name: "工作区一" }),
        gatewayWorkspace({ workspace_id: "ws-2", name: "工作区二" }),
      ]),
    );
    const list = spyOnGatewayApi("listGatewayWorkspaces");
    // 回退目标不在返回列表里，用于确认根路径与名称确实沿用 prev。
    const { hook, state } = await mountHook(appState({
      activeGatewayWorkspaceId: "ws-fallback",
      workspaceRoot: "/tmp/fallback-root",
      workspaceName: "回退工作区",
    }));

    await hook.reorderGatewayWorkspaces(["ws-2", "ws-1"]);

    expect(reorder).toHaveBeenCalledWith(API_PORT, { workspace_ids: ["ws-2", "ws-1"] });
    expect(list).not.toHaveBeenCalled();
    const next = state();
    expect(next.activeGatewayWorkspaceId).toBe("ws-fallback");
    expect(next.workspaceRoot).toBe("/tmp/fallback-root");
    expect(next.workspaceName).toBe("回退工作区");
    expect(next.gatewayWorkspaces.map((item) => item.workspace_id)).toEqual(["ws-1", "ws-2"]);
    expect(next.status).toBe("工作区顺序已更新");
  });

  test("排序失败只写 gatewayError 与 status，不占用初始化失败通道，并原样抛出", async () => {
    const failure = new Error("排序后端不可用");
    const reorder = spyOnGatewayApi("reorderGatewayWorkspaces").mockRejectedValue(failure);
    const list = spyOnGatewayApi("listGatewayWorkspaces");
    const { hook, state } = await mountHook(appState());

    await expect(hook.reorderGatewayWorkspaces(["ws-2", "ws-1"])).rejects.toBe(failure);

    expect(reorder).toHaveBeenCalledTimes(1);
    expect(list).not.toHaveBeenCalled();
    const next = state();
    expect(next.gatewayError).toBe("排序后端不可用");
    expect(next.error).toBeNull();
    expect(next.status).toBe("工作区排序失败: 排序后端不可用");
  });
});

describe("Gateway 工作区重命名", () => {
  test("重命名响应缺少工作区时抛出明确错误并回滚读取工作区列表", async () => {
    spyOnGatewayApi("renameGatewayWorkspace").mockResolvedValue(
      workspaceList("ws-active", [gatewayWorkspace({ workspace_id: "ws-2", name: "工作区二" })]),
    );
    const list = spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
      workspaceList("ws-2", [gatewayWorkspace({ workspace_id: "ws-2", name: "工作区二", root_path: "/tmp/two" })]),
    );
    const { hook, state } = await mountHook(appState());

    await expect(hook.renameGatewayWorkspace("ws-1", "新名字")).rejects.toThrow(
      "Gateway 重命名响应缺少工作区: ws-1",
    );

    expect(list).toHaveBeenCalledWith(API_PORT);
    const next = state();
    expect(next.gatewayError).toBe("Gateway 重命名响应缺少工作区: ws-1");
    expect(next.status).toBe("重命名工作区失败: Gateway 重命名响应缺少工作区: ws-1");
    expect(next.activeGatewayWorkspaceId).toBe("ws-2");
    expect(next.workspaceRoot).toBe("/tmp/two");
    expect(next.workspaceName).toBe("工作区二");
  });

  test("重命名失败后重取工作区列表完成回滚", async () => {
    const failure = new Error("名称冲突");
    spyOnGatewayApi("renameGatewayWorkspace").mockRejectedValue(failure);
    spyOnGatewayApi("listGatewayWorkspaces").mockResolvedValue(
      workspaceList("ws-active", [
        gatewayWorkspace({ workspace_id: "ws-active", name: "回滚后活动工作区", root_path: "/tmp/rollback" }),
      ]),
    );
    const { hook, state } = await mountHook(appState());

    await expect(hook.renameGatewayWorkspace("ws-1", "新名字")).rejects.toThrow("名称冲突");

    const next = state();
    expect(next.gatewayError).toBe("名称冲突");
    expect(next.status).toBe("重命名工作区失败: 名称冲突");
    expect(next.workspaceRoot).toBe("/tmp/rollback");
    expect(next.workspaceName).toBe("回滚后活动工作区");
  });

  test("重命名失败且回滚也失败时拼接复合文案", async () => {
    spyOnGatewayApi("renameGatewayWorkspace").mockRejectedValue(new Error("名称冲突"));
    spyOnGatewayApi("listGatewayWorkspaces").mockRejectedValue(new Error("列表不可用"));
    const { hook, state } = await mountHook(appState());

    await expect(hook.renameGatewayWorkspace("ws-1", "新名字")).rejects.toThrow(
      "名称冲突；重新读取工作区列表也失败: 列表不可用",
    );

    const next = state();
    expect(next.gatewayError).toBe("名称冲突；重新读取工作区列表也失败: 列表不可用");
    expect(next.error).toBeNull();
    expect(next.status).toBe("重命名工作区失败: 名称冲突；重新读取工作区列表也失败: 列表不可用");
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
        expanded_paths_by_workspace: { "ws-active": ["/active"] },
      },
    });
  });

  test("删除活动工作区清空会话级状态并标记正在切换", async () => {
    spyOnGatewayApi("removeGatewayWorkspace").mockResolvedValue(
      workspaceList("ws-2", [
        gatewayWorkspace({ workspace_id: "ws-2", name: "新活动工作区", root_path: "/tmp/two" }),
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
    expect(next.workspaceRoot).toBe("/tmp/two");
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
        finishWorkspaceRefresh: async () => {
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
    expect([...next.removingGatewayWorkspaceIds]).toEqual(["ws-active"]);
    expect(next.workspaceSwitching).toBe(false);
  });
});

describe("Gateway 受管工作区新增", () => {
  const addPayload = { root_path: "/tmp/new-workspace" };

  test("根路径为空时跳过最近路径写入", async () => {
    spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    const { hook, calls } = await mountHook(appState(), {
      recentLocalWorkspacePaths: ["/tmp/old-workspace"],
    });

    await hook.addManagedGatewayWorkspace({ root_path: "   " });

    expect(calls.updates).toEqual([]);
    expect(calls.finish).toBe(1);
  });

  test("带 gateway_connection_id 时跳过最近路径写入", async () => {
    spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    const { hook, calls } = await mountHook(appState(), {
      recentLocalWorkspacePaths: ["/tmp/old-workspace"],
    });

    await hook.addManagedGatewayWorkspace({
      root_path: "/tmp/new-workspace",
      gateway_connection_id: "conn-1",
    });

    expect(calls.updates).toEqual([]);
    expect(calls.finish).toBe(1);
  });

  test("最近路径按首次出现去重并剔除空白项", async () => {
    spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    const { hook, calls } = await mountHook(appState(), {
      recentLocalWorkspacePaths: ["/tmp/a", "/tmp/b", "   ", "/tmp/a"],
    });

    await hook.addManagedGatewayWorkspace({ root_path: "  /tmp/b  " });

    expect(calls.updates).toEqual([
      { recent_local_workspace_paths: ["/tmp/b", "/tmp/a"] },
    ]);
    expect(calls.finish).toBe(1);
  });

  test("最近路径写入与工作区刷新同时失败时聚合两条错误", async () => {
    spyOnGatewayApi("addManagedGatewayWorkspace").mockResolvedValue({} as never);
    const { hook, state } = await mountHook(appState(), {
      recentLocalWorkspacePaths: [],
      updateUiSettings: async () => {
        throw new Error("设置写入失败");
      },
      finishWorkspaceRefresh: async () => {
        throw new Error("刷新失败");
      },
    });

    const message = "工作区已添加，但界面同步失败: 保存最近路径失败: 设置写入失败；刷新工作区列表失败: 刷新失败";
    await expect(hook.addManagedGatewayWorkspace(addPayload)).rejects.toThrow(message);

    const next = state();
    expect(next.gatewayError).toBe(message);
    expect(next.error).toBeNull();
    expect(next.status).toBe(message);
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
  });

  test("新增受管工作区接口失败时写入添加失败文案并原样抛出", async () => {
    const failure = new Error("后端拒绝");
    spyOnGatewayApi("addManagedGatewayWorkspace").mockRejectedValue(failure);
    const { hook, state, calls } = await mountHook(appState(), {
      recentLocalWorkspacePaths: ["/tmp/a"],
    });

    await expect(hook.addManagedGatewayWorkspace(addPayload)).rejects.toBe(failure);

    const next = state();
    expect(next.gatewayError).toBe("后端拒绝");
    expect(next.error).toBeNull();
    expect(next.status).toBe("添加工作区失败: 后端拒绝");
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
    expect(calls.updates).toEqual([]);
    expect(calls.finish).toBe(0);
  });
});

describe("Gateway 远程工作区新增", () => {
  test("调用接口前先同步重置工作区级状态，失败时写入连接失败文案", async () => {
    const events: string[] = [];
    const failure = new Error("连接被拒绝");
    spyOnGatewayApi("addSshGatewayWorkspace").mockImplementation(async () => {
      events.push("api");
      throw failure;
    });
    const { hook, state, calls } = await mountHook(appState(), {
      resetWorkspaceScopedState: () => { events.push("reset"); },
    });

    await expect(hook.addSshGatewayWorkspace({
      remote_gateway_port: 8014,
      ssh_config_host: "remote-host",
    })).rejects.toBe(failure);

    expect(events).toEqual(["reset", "api"]);
    expect(calls.reset).toBe(1);
    expect(calls.finish).toBe(0);
    const next = state();
    expect(next.gatewayError).toBe("连接被拒绝");
    expect(next.error).toBeNull();
    expect(next.status).toBe("连接远程 Gateway 失败: 连接被拒绝");
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
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

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceMutations({
      apiPort: null,
      activeGatewayWorkspaceId: "ws-active",
      recentLocalWorkspacePaths: [],
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
