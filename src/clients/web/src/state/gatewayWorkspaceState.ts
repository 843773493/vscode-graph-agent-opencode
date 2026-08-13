import type { GatewayWorkspaceList, Session } from "../types/backend";

interface GatewayWorkspaceRemovalState {
  gatewayWorkspaces: GatewayWorkspaceList["items"];
  activeGatewayWorkspaceId: string | null;
  sessionsByWorkspace: Map<string, Session[]>;
  sessionGatewayWorkspaceById: Map<string, string>;
  removingGatewayWorkspaceIds: Set<string>;
  gatewayError: string | null;
  error: string | null;
  status: string;
}

export function applyGatewayWorkspaceListAfterRemoval<
  State extends GatewayWorkspaceRemovalState,
>(
  state: State,
  removedWorkspaceId: string,
  workspaceList: GatewayWorkspaceList,
): State {
  const sessionsByWorkspace = new Map(state.sessionsByWorkspace);
  sessionsByWorkspace.delete(removedWorkspaceId);

  const sessionGatewayWorkspaceById = new Map(
    [...state.sessionGatewayWorkspaceById].filter(
      ([, workspaceId]) => workspaceId !== removedWorkspaceId,
    ),
  );
  const removingGatewayWorkspaceIds = new Set(
    state.removingGatewayWorkspaceIds,
  );
  removingGatewayWorkspaceIds.delete(removedWorkspaceId);

  return {
    ...state,
    gatewayWorkspaces: workspaceList.items,
    activeGatewayWorkspaceId: workspaceList.active_workspace_id,
    sessionsByWorkspace,
    sessionGatewayWorkspaceById,
    removingGatewayWorkspaceIds,
    gatewayError: null,
    error: null,
    status: "工作区已删除",
  };
}
