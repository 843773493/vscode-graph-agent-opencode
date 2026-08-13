import { useCallback } from "react";
import {
  buildWorkspaceInformationDump,
  formatWorkspaceInformationDump,
} from "../state/workspaceInformation";
import type { GatewayWorkspace } from "../types/backend";
import { copyTextToClipboard } from "../utils/clipboard";

export function useWorkspaceInformationClipboard(
  workspaces: GatewayWorkspace[],
) {
  return useCallback(
    async (workspaceId: string): Promise<void> => {
      const workspace = workspaces.find(
        (candidate) => candidate.workspace_id === workspaceId,
      );
      if (!workspace) {
        throw new Error(`Gateway 不存在目标工作区: ${workspaceId}`);
      }
      const dump = buildWorkspaceInformationDump(workspace, workspaces);
      await copyTextToClipboard(formatWorkspaceInformationDump(dump));
    },
    [workspaces],
  );
}
