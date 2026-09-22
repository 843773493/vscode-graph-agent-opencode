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

interface GatewayWorkspaceListSlice {
  gatewayWorkspaces: GatewayWorkspaceList["items"];
  gatewayWorkspacesStale?: boolean;
}

/** 写入一份新的 Gateway 工作区权威列表：服务端已确认的列表一律清除失效标记，
 * 保证 gatewayWorkspacesStale 只反映最近一次读取结果，不会残留成假阳性。 */
export function withFreshGatewayWorkspaceList<
  State extends GatewayWorkspaceListSlice,
>(state: State, items: GatewayWorkspaceList["items"]): State {
  return { ...state, gatewayWorkspaces: items, gatewayWorkspacesStale: false };
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
    ...withFreshGatewayWorkspaceList(state, workspaceList.items),
    activeGatewayWorkspaceId: workspaceList.active_workspace_id,
    sessionsByWorkspace,
    sessionGatewayWorkspaceById,
    removingGatewayWorkspaceIds,
    gatewayError: null,
    error: null,
    status: "工作区已删除",
  };
}
