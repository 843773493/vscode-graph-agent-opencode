import { gatewayWorkspaceKey, workspaceOwnerFromKey } from "../../api/workspaceIdentity";
import { useCallback, type MutableRefObject } from "react";
import {
  listGatewayWorkspaces,
  updateGatewayWorkspace,
} from "../../gatewayApi";
import type { RunGatewayWorkspaceListTask, SetAppState } from "../contentViewLoaderTypes";
import { withFreshGatewayWorkspaceList } from "../../state/gatewayWorkspaceState";
import { errorMessage } from "../../utils/errorMessage";
import { t } from "../../../../shared/core/i18n/index.js";
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
          const parent = parentWorkspaceId ? workspaceOwnerFromKey(parentWorkspaceId) : null;
          const workspaceList = await updateGatewayWorkspace(
            apiPort,
            workspaceId,
            { parent_workspace: parent ? { gateway_id: parent.gatewayId, workspace_id: parent.workspaceId } : null },
          );
          const updatedWorkspace = workspaceList.items.find(
            (workspace) => gatewayWorkspaceKey(workspace) === workspaceId,
          );
          if (!updatedWorkspace) {
            throw new Error(t("gateway.workspace.updatedWorkspaceMissing", workspaceId));
          }
          setState((previous) => ({
            ...withFreshGatewayWorkspaceList(previous, workspaceList.items),
            ...(selectionIntent === selectionIntentRef.current ? {
              ...activeGatewayWorkspaceFields(workspaceList),
              gatewayError: null,
              error: null,
              status: parentWorkspaceId
                ? t("gateway.workspace.movedIntoParent", updatedWorkspace.name)
                : t("gateway.workspace.movedOutOfParent", updatedWorkspace.name),
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
            message = t("gateway.workspace.operationAndReadbackFailed", operationMessage, reconciliationMessage);
            gatewayWorkspacesStale = true;
          }
          setState((previous) => ({
            ...previous,
            ...(gatewayWorkspacesStale ? { gatewayWorkspacesStale: true } : {}),
            ...(selectionIntent === selectionIntentRef.current ? {
              gatewayError: message,
              status: t("gateway.workspace.updateHierarchyFailed", message),
            } : {}),
          }));
          throw error;
        }
      });
    },
    [apiPort, gatewayListRequestRef, runGatewayWorkspaceListTask, selectionIntentRef, setState],
  );
}
