import { useCallback } from "react";
import {
  listGatewayWorkspaces,
  updateGatewayWorkspace,
} from "../../gatewayApi";
import type { SetAppState } from "../contentViewLoaderTypes";
import { withFreshGatewayWorkspaceList } from "../../state/gatewayWorkspaceState";
import { errorMessage } from "../../utils/errorMessage";
import { activeGatewayWorkspaceFields } from "./activeGatewayWorkspaceFields";

export function useGatewayWorkspaceHierarchy(
  apiPort: number,
  setState: SetAppState,
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
          return {
            ...withFreshGatewayWorkspaceList(previous, workspaceList.items),
            ...activeGatewayWorkspaceFields(workspaceList),
            gatewayError: null,
            error: null,
            status: parentWorkspaceId
              ? `工作区「${updatedWorkspace.name}」已移入父工作区`
              : `工作区「${updatedWorkspace.name}」已移出父工作区`,
          };
        });
      } catch (error) {
        const operationMessage =
          errorMessage(error);
        let message = operationMessage;
        try {
          const workspaceList = await listGatewayWorkspaces(apiPort);
          setState((previous) => ({
            ...withFreshGatewayWorkspaceList(previous, workspaceList.items),
            ...activeGatewayWorkspaceFields(workspaceList),
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`;
        }
        setState((previous) => ({
          ...previous,
          gatewayError: message,
          status: `更新工作区父子关系失败: ${message}`,
        }));
        throw new Error(message);
      }
    },
    [apiPort, setState],
  );
}
