import { applyGatewayWorkspaceListAfterRemoval } from "../gatewayWorkspaceState";
import type { GatewayWorkspace, Session } from "../../types/backend";

function workspace(workspaceId: string, active: boolean): GatewayWorkspace {
  return {
    workspace_id: workspaceId,
    name: workspaceId,
    root_path: `/tmp/${workspaceId}`,
    backend_url: "http://127.0.0.1:8010",
    connection_kind: "local",
    status: "ready",
    active,
    managed: false,
    removable: true,
    system_default: false,
    remote: {},
    services: {},
    checked_at: "2026-07-16T00:00:00Z",
  };
}

function session(sessionId: string): Session {
  return {
    session_id: sessionId,
    workspace_id: "workspace",
    title: sessionId,
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-13T00:00:00Z",
    updated_at: "2026-07-13T00:00:00Z",
  };
}

{
  const keptSession = session("kept-session");
  const removedSession = session("removed-session");
  const previous = {
    gatewayWorkspaces: [workspace("kept", true), workspace("removed", false)],
    activeGatewayWorkspaceId: "kept",
    sessionsByWorkspace: new Map([
      ["kept", [keptSession]],
      ["removed", [removedSession]],
    ]),
    sessionGatewayWorkspaceById: new Map([
      ["kept:kept-session", "kept"],
      ["removed:removed-session", "removed"],
    ]),
    removingGatewayWorkspaceIds: new Set(["removed"]),
    gatewayError: "旧错误",
    error: "旧错误",
    status: "正在删除工作区",
    unrelatedValue: "保持不变",
  };

  const next = applyGatewayWorkspaceListAfterRemoval(previous, "removed", {
    active_workspace_id: "kept",
    items: [workspace("kept", true)],
  });

  if (next.gatewayWorkspaces.map((item) => item.workspace_id).join(",") !== "kept") {
    throw new Error("删除后未采用后端返回的完整工作区列表");
  }
  if ([...next.sessionsByWorkspace.keys()].join(",") !== "kept") {
    throw new Error("删除后未清理目标工作区的会话缓存");
  }
  if ([...next.sessionGatewayWorkspaceById.values()].join(",") !== "kept") {
    throw new Error("删除后未清理目标工作区的会话索引");
  }
  if (next.removingGatewayWorkspaceIds.size !== 0) {
    throw new Error("删除成功后仍保留正在删除标记");
  }
  if (next.gatewayError !== null || next.error !== null) {
    throw new Error("删除成功后未清理旧错误");
  }
  if (next.status !== "工作区已删除") {
    throw new Error("删除成功状态错误");
  }
  if (next.unrelatedValue !== "保持不变") {
    throw new Error("删除局部状态时修改了无关状态");
  }
}
