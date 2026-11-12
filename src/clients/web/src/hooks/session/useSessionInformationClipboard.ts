import { t } from '../../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { useCallback } from "react";
import { getSessionInformation } from "../../api";
import { listGatewayWorkspaces } from "../../gatewayApi";
import {
  buildSessionInformationDump,
  formatSessionInformationDump,
} from "../../state/session/sessionInformation";
import { copyTextToClipboardFromPromise } from "../../utils/clipboard";

export function useSessionInformationClipboard(apiPort: number) {
  return useCallback(
    async (workspaceId: string, sessionId: string): Promise<void> => {
      const textPromise = Promise.all([
        getSessionInformation(apiPort, sessionId, workspaceId),
        listGatewayWorkspaces(apiPort),
      ]).then(([information, workspaceList]) => {
        const gatewayWorkspace = workspaceList.items.find(
          (workspace) => gatewayWorkspaceKey(workspace) === workspaceId,
        );
        if (!gatewayWorkspace) {
          throw new Error(t('navigation.workspace.notFound', workspaceId));
        }

        const dump = buildSessionInformationDump(information, gatewayWorkspace);
        return formatSessionInformationDump(dump);
      });
      await copyTextToClipboardFromPromise(textPromise);
    },
    [apiPort],
  );
}
