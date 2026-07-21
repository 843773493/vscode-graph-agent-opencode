import { useCallback, type Dispatch, type SetStateAction } from "react";
import {
  listGatewayWorkspaces,
  updateGatewayWorkspace,
} from "../gatewayApi";
import type { AppState } from "../types/frontend";

export function useGatewayWorkspaceHierarchy(
  apiPort: number,
  setState: Dispatch<SetStateAction<AppState>>,
) {
  return useCallback(
    async (
      workspaceId: string,
      parentWorkspaceId: string | null,
    ): Promise<void> => {
      try {
        const workspaceList = await updateGatewayWorkspace(
          apiPort,
          workspaceId,
          { parent_workspace_id: parentWorkspaceId },
        );
        const updatedWorkspace = workspaceList.items.find(
          (workspace) => workspace.workspace_id === workspaceId,
        );
        if (!updatedWorkspace) {
          throw new Error(`Gateway 更新响应缺少工作区: ${workspaceId}`);
        }
        setState((previous) => {
          const activeWorkspace = workspaceList.items.find(
            (workspace) =>
              workspace.workspace_id === workspaceList.active_workspace_id,
          );
          return {
            ...previous,
            gatewayWorkspaces: workspaceList.items,
            activeGatewayWorkspaceId: workspaceList.active_workspace_id,
            workspaceRoot: activeWorkspace?.root_path ?? null,
            workspaceName: activeWorkspace?.name ?? null,
            gatewayError: null,
            error: null,
            status: parentWorkspaceId
              ? `工作区「${updatedWorkspace.name}」已移入父工作区`
              : `工作区「${updatedWorkspace.name}」已移出父工作区`,
          };
        });
      } catch (error) {
        const operationMessage =
          error instanceof Error ? error.message : String(error);
        let message = operationMessage;
        try {
          const workspaceList = await listGatewayWorkspaces(apiPort);
          setState((previous) => ({
            ...previous,
            gatewayWorkspaces: workspaceList.items,
            activeGatewayWorkspaceId: workspaceList.active_workspace_id,
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = reconciliationError instanceof Error
            ? reconciliationError.message
            : String(reconciliationError);
          message = `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`;
        }
        setState((previous) => ({
          ...previous,
          gatewayError: message,
          error: message,
          status: `更新工作区父子关系失败: ${message}`,
        }));
        throw new Error(message);
      }
    },
    [apiPort, setState],
  );
}
