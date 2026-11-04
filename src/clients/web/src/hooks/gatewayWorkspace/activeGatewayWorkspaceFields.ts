import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../api/workspaceIdentity";
import type { GatewayWorkspaceList } from "../../types/backend";

/** 活动工作区的身份和名称随权威列表整体替换。 */
export function activeGatewayWorkspaceFields(workspaceList: GatewayWorkspaceList) {
  const active = workspaceList.items.find(
    (item) => gatewayWorkspaceKey(item) === nullableGatewayWorkspaceKey(workspaceList.active_workspace),
  );
  return {
    activeGatewayWorkspaceId: nullableGatewayWorkspaceKey(workspaceList.active_workspace),
    workspaceName: active?.name ?? null,
  };
}
