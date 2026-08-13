import { useCallback } from "react";
import { getSessionInformation } from "../api";
import { listGatewayWorkspaces } from "../gatewayApi";
import {
  buildSessionInformationDump,
  formatSessionInformationDump,
} from "../state/session/sessionInformation";
import { copyTextToClipboard } from "../utils/clipboard";

export function useSessionInformationClipboard(apiPort: number) {
  return useCallback(
    async (workspaceId: string, sessionId: string): Promise<void> => {
      const [information, workspaceList] = await Promise.all([
        getSessionInformation(apiPort, sessionId, workspaceId),
        listGatewayWorkspaces(apiPort),
      ]);
      const gatewayWorkspace = workspaceList.items.find(
        (workspace) => workspace.workspace_id === workspaceId,
      );
      if (!gatewayWorkspace) {
        throw new Error(`Gateway 不存在目标工作区: ${workspaceId}`);
      }

      const dump = buildSessionInformationDump(information, gatewayWorkspace);
      await copyTextToClipboard(formatSessionInformationDump(dump));
    },
    [apiPort],
  );
}
