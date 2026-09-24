import type { GatewayWorkspaceList } from "../../types/backend";

/** 活动工作区派生字段的唯一推导：id、根路径与名称必须整体替换，只改其中一个会让
 * 文件树按新工作区 id 去读旧根路径。此前这段推导在层级与增删改链路里重复了六处。 */
export function activeGatewayWorkspaceFields(workspaceList: GatewayWorkspaceList) {
  const active = workspaceList.items.find(
    (item) => item.workspace_id === workspaceList.active_workspace_id,
  );
  return {
    activeGatewayWorkspaceId: workspaceList.active_workspace_id,
    workspaceRoot: active?.root_path ?? null,
    workspaceName: active?.name ?? null,
  };
}
