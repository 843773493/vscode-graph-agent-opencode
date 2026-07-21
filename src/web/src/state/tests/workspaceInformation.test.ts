import {
  buildWorkspaceInformationDump,
  extractWorkspaceIdFromClipboardText,
  formatWorkspaceInformationDump,
} from "../workspaceInformation";
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
const dump = buildWorkspaceInformationDump(parent, [parent, child]);
if (dump.workspace.child_workspace_ids.join(",") !== "gw_child") {
  throw new Error("复制工作区信息未包含直接子工作区");
}
if (extractWorkspaceIdFromClipboardText(formatWorkspaceInformationDump(dump)) !== "gw_parent") {
  throw new Error("无法从通用工作区信息提取工作区 ID");
}
if (extractWorkspaceIdFromClipboardText("gw_child") !== "gw_child") {
  throw new Error("无法从纯工作区 ID 提取工作区");
}
