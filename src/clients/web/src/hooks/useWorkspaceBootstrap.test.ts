import { describe, expect, test } from "bun:test";
import {
  selectBootstrapSessionId,
  selectBootstrapToolDetailsExpanded,
  canAcceptUserViewStateResponse,
  canAcceptUserViewStateMutation,
  shouldRestorePersistedWorkspace,
  isRetryableWorkspaceBootstrapError,
  selectHealthyGatewayWorkspace,
} from "./useWorkspaceBootstrap";
import { HttpRequestError } from "../api/http";
import type { GatewayWorkspace } from "../types/backend";

describe("selectBootstrapSessionId", () => {
  test("用户切换后不沿用上一个用户的当前会话", () => {
    expect(selectBootstrapSessionId({
      preferredSessionId: "session-a",
      previousSessionId: "session-a",
      userChanged: true,
    })).toBeNull();
  });

  test("优先使用用户服务端保存的会话位置", () => {
    expect(selectBootstrapSessionId({
      persistedSessionId: "session-b",
      previousSessionId: "session-a",
      userChanged: true,
    })).toBe("session-b");
  });

  test("同一用户刷新时保留当前会话", () => {
    expect(selectBootstrapSessionId({
      previousSessionId: "session-a",
      userChanged: false,
    })).toBe("session-a");
  });

  test("切换用户时使用服务端保存的工具详情状态", () => {
    expect(selectBootstrapToolDetailsExpanded({
      persistedToolDetailsExpanded: true,
      previousToolDetailsExpanded: false,
      userChanged: true,
    })).toBe(true);
    expect(selectBootstrapToolDetailsExpanded({
      persistedToolDetailsExpanded: false,
      previousToolDetailsExpanded: true,
      userChanged: true,
    })).toBe(false);
  });

  test("同一用户刷新时不覆盖当前内存中的工具详情状态", () => {
    expect(selectBootstrapToolDetailsExpanded({
      persistedToolDetailsExpanded: false,
      previousToolDetailsExpanded: true,
      userChanged: false,
    })).toBe(true);
  });
});

describe("canAcceptUserViewStateResponse", () => {
  test("只接受当前用户的异步视图状态响应", () => {
    expect(canAcceptUserViewStateResponse("user-a", "user-a")).toBe(true);
    expect(canAcceptUserViewStateResponse("user-b", "user-a")).toBe(false);
    expect(canAcceptUserViewStateResponse(null, "user-a")).toBe(false);
  });
});

describe("canAcceptUserViewStateMutation", () => {
  test("接管后同一用户 ID 的旧请求结果也必须丢弃", () => {
    expect(canAcceptUserViewStateMutation({
      currentUserId: "alice",
      responseUserId: "alice",
      currentLeaseGeneration: 2,
      requestLeaseGeneration: 1,
    })).toBe(false);
  });

  test("同一租约代数的当前用户请求结果可以接受", () => {
    expect(canAcceptUserViewStateMutation({
      currentUserId: "alice",
      responseUserId: "alice",
      currentLeaseGeneration: 2,
      requestLeaseGeneration: 2,
    })).toBe(true);
  });
});

describe("shouldRestorePersistedWorkspace", () => {
  const userViewState = {
    user_id: "alice",
    workspace_id: "workspace-b",
    session_id: "session-b",
    turn_anchor: "turn-b",
    scroll_offset: 0,
    follow_latest: true,
    projection_version: 1,
    tool_details_expanded: false,
    updated_at: "2026-08-27T00:00:00Z",
  };

  test("首次 bootstrap 且持久化工作区可用时才恢复", () => {
    expect(shouldRestorePersistedWorkspace({
      restorePersistedWorkspace: true,
      userViewState,
      activeWorkspaceId: "workspace-a",
      availableWorkspaceIds: ["workspace-a", "workspace-b"],
    })).toBe(true);
  });

  test("显式切换后的刷新不能把工作区切回持久化位置", () => {
    expect(shouldRestorePersistedWorkspace({
      restorePersistedWorkspace: false,
      userViewState,
      activeWorkspaceId: "workspace-b",
      availableWorkspaceIds: ["workspace-a", "workspace-b"],
    })).toBe(false);
  });

  test("持久化工作区已是当前工作区或不可用时不执行恢复", () => {
    expect(shouldRestorePersistedWorkspace({
      restorePersistedWorkspace: true,
      userViewState,
      activeWorkspaceId: "workspace-b",
      availableWorkspaceIds: ["workspace-a", "workspace-b"],
    })).toBe(false);
    expect(shouldRestorePersistedWorkspace({
      restorePersistedWorkspace: true,
      userViewState,
      activeWorkspaceId: "workspace-a",
      availableWorkspaceIds: ["workspace-a"],
    })).toBe(false);
  });
});

describe("isRetryableWorkspaceBootstrapError", () => {
  test("工作区后端尚未就绪的 503 可以重试", () => {
    expect(isRetryableWorkspaceBootstrapError(
      new HttpRequestError(503, "Service Unavailable", "not ready", "/api/v1/workspace"),
    )).toBe(true);
  });

  test("业务鉴权失败不应被初始化重试吞掉", () => {
    expect(isRetryableWorkspaceBootstrapError(
      new HttpRequestError(401, "Unauthorized", "expired", "/api/v1/sessions"),
    )).toBe(false);
  });
});

describe("selectHealthyGatewayWorkspace", () => {
  function workspace(overrides: Partial<GatewayWorkspace>): GatewayWorkspace {
    return {
      workspace_id: "workspace-ready",
      parent_workspace_id: null,
      name: "Ready",
      root_path: "/tmp/ready",
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

  const ready = workspace({});
  const defaultWorkspace = workspace({
    workspace_id: "workspace-default",
    name: "Default",
    root_path: "/tmp/default",
    system_default: true,
  });
  const offline = workspace({
    workspace_id: "workspace-offline",
    name: "Offline",
    root_path: "/tmp/offline",
    status: "offline",
  });

  test("活动工作区离线时优先切换到健康的系统默认工作区", () => {
    expect(selectHealthyGatewayWorkspace({
      active_workspace_id: offline.workspace_id,
      items: [offline, ready, defaultWorkspace],
    })).toBe(defaultWorkspace.workspace_id);
  });

  test("没有健康工作区时返回 null", () => {
    expect(selectHealthyGatewayWorkspace({
      active_workspace_id: offline.workspace_id,
      items: [offline],
    })).toBeNull();
  });

  test("活动工作区健康时保持用户当前选择", () => {
    expect(selectHealthyGatewayWorkspace({
      active_workspace_id: ready.workspace_id,
      items: [defaultWorkspace, ready],
    })).toBe(ready.workspace_id);
  });
});
