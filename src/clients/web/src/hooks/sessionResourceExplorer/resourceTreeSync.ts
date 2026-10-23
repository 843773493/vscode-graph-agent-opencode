import type { GatewayWorkspace, Session } from "../../types/backend";

export function buildWorkspaceNavigationSyncKey(
  workspaces: readonly GatewayWorkspace[],
): string {
  return workspaces
    .map((workspace) => `${workspace.workspace_id}\u0000${workspace.name}`)
    .sort()
    .join("\u0001");
}

export function buildSessionCatalogSyncKeys(
  sessionsByWorkspace: ReadonlyMap<string, readonly Session[]>,
): ReadonlyMap<string, string> {
  return new Map(
    [...sessionsByWorkspace.entries()].map(([workspaceId, sessions]) => [
      workspaceId,
      sessions
        .map((session) => [
          session.session_id,
          session.title,
          session.parent_session_id ?? "",
        ].join("\u0000"))
        .sort()
        .join("\u0001"),
    ]),
  );
}

export function changedCatalogWorkspaceIds(
  previousSyncKeys: ReadonlyMap<string, string>,
  nextSyncKeys: ReadonlyMap<string, string>,
  previousRefreshVersions: ReadonlyMap<string, number>,
  nextRefreshVersions: ReadonlyMap<string, number>,
): string[] {
  const workspaceIds = new Set(previousSyncKeys.keys());
  for (const workspaceId of nextSyncKeys.keys()) workspaceIds.add(workspaceId);
  for (const workspaceId of previousRefreshVersions.keys()) workspaceIds.add(workspaceId);
  for (const workspaceId of nextRefreshVersions.keys()) workspaceIds.add(workspaceId);

  const changedWorkspaceIds: string[] = [];
  for (const workspaceId of workspaceIds) {
    if (previousSyncKeys.get(workspaceId) !== nextSyncKeys.get(workspaceId)
      || previousRefreshVersions.get(workspaceId)
        !== nextRefreshVersions.get(workspaceId)) {
      changedWorkspaceIds.push(workspaceId);
    }
  }
  return changedWorkspaceIds;
}
