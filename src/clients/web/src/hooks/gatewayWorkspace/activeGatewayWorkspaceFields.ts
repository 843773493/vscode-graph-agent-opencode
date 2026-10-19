import type { GatewayWorkspaceList } from "../../types/backend";

/** 活动工作区的身份和名称随权威列表整体替换。 */
export function activeGatewayWorkspaceFields(workspaceList: GatewayWorkspaceList) {
  const active = workspaceList.items.find(
    (item) => item.workspace_id === workspaceList.active_workspace_id,
  );
  return {
    activeGatewayWorkspaceId: workspaceList.active_workspace_id,
    workspaceName: active?.name ?? null,
  };
}
