import { buildVisibleWorkspaceTree } from "../../components/agentSessions/agentSessionsUtils";
import type { GatewayWorkspace } from "../../types/backend";

function workspace(
  workspaceId: string,
  parentWorkspaceId: string | null = null,
): GatewayWorkspace {
  return {
    workspace_id: workspaceId,
    parent_workspace_id: parentWorkspaceId,
    name: workspaceId,
    root_path: `/workspace/${workspaceId}`,
    backend_url: "http://127.0.0.1:8010",
    connection_kind: "local",
    status: "ready",
    active: false,
    managed: true,
    removable: true,
    system_default: false,
    runtime_action: "safe_restart_managed_backend",
    config_reload: {
      available: true,
      healthy: true,
      restart_required: false,
      changed_sections: [],
    },
    remote: null,
    services: {},
    checked_at: "2026-07-17T00:00:00Z",
  };
}

const parent = workspace("gw_parent");
const child = workspace("gw_child", "gw_parent");
const sibling = workspace("gw_sibling");
const expanded = buildVisibleWorkspaceTree(
  [parent, child, sibling],
  new Set(),
);
if (
  expanded.map((node) => `${node.workspace.workspace_id}:${node.depth}`).join(",")
  !== "gw_parent:0,gw_child:1,gw_sibling:0"
) {
  throw new Error("工作区树未按父子关系和原始顺序展开");
}

const collapsed = buildVisibleWorkspaceTree(
  [parent, child, sibling],
  new Set(["gw_parent"]),
);
if (
  collapsed.map((node) => node.workspace.workspace_id).join(",")
  !== "gw_parent,gw_sibling"
) {
  throw new Error("折叠父工作区后仍显示子工作区");
}
