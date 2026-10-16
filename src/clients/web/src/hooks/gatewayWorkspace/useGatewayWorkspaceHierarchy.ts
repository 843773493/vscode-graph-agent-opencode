import { useCallback, type MutableRefObject } from "react";
import {
  listGatewayWorkspaces,
  updateGatewayWorkspace,
} from "../../gatewayApi";
import type { RunGatewayWorkspaceListTask, SetAppState } from "../contentViewLoaderTypes";
import { withFreshGatewayWorkspaceList } from "../../state/gatewayWorkspaceState";
import { errorMessage } from "../../utils/errorMessage";
import { activeGatewayWorkspaceFields } from "./activeGatewayWorkspaceFields";

export function useGatewayWorkspaceHierarchy(
  apiPort: number,
  setState: SetAppState,
  gatewayListRequestRef: MutableRefObject<number>,
  runGatewayWorkspaceListTask: RunGatewayWorkspaceListTask,
  selectionIntentRef: MutableRefObject<number>,
) {
  return useCallback(
    async (
      workspaceId: string,
      parentWorkspaceId: string | null,
    ): Promise<void> => {
      gatewayListRequestRef.current += 1;
      const selectionIntent = selectionIntentRef.current;
      await runGatewayWorkspaceListTask(async () => {
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
          setState((previous) => ({
            ...withFreshGatewayWorkspaceList(previous, workspaceList.items),
            ...(selectionIntent === selectionIntentRef.current ? {
              ...activeGatewayWorkspaceFields(workspaceList),
              gatewayError: null,
              error: null,
              status: parentWorkspaceId
                ? `工作区「${updatedWorkspace.name}」已移入父工作区`
                : `工作区「${updatedWorkspace.name}」已移出父工作区`,
            } : {}),
          }));
        } catch (error) {
          const operationMessage = errorMessage(error);
          let message = operationMessage;
          let gatewayWorkspacesStale = false;
          try {
            const workspaceList = await listGatewayWorkspaces(apiPort);
            setState((previous) => ({
              ...withFreshGatewayWorkspaceList(previous, workspaceList.items),
              ...(selectionIntent === selectionIntentRef.current ? activeGatewayWorkspaceFields(workspaceList) : {}),
            }));
          } catch (reconciliationError) {
            const reconciliationMessage = errorMessage(reconciliationError);
            message = `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`;
            gatewayWorkspacesStale = true;
          }
          setState((previous) => ({
            ...previous,
            ...(gatewayWorkspacesStale ? { gatewayWorkspacesStale: true } : {}),
            ...(selectionIntent === selectionIntentRef.current ? {
              gatewayError: message,
              status: `更新工作区父子关系失败: ${message}`,
            } : {}),
          }));
          throw error;
        }
      });
    },
    [apiPort, gatewayListRequestRef, runGatewayWorkspaceListTask, selectionIntentRef, setState],
  );
}
