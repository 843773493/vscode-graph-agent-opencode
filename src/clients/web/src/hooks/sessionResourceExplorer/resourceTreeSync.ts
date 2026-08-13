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
  const workspaceIds = new Set([
    ...previousSyncKeys.keys(),
    ...nextSyncKeys.keys(),
    ...previousRefreshVersions.keys(),
    ...nextRefreshVersions.keys(),
  ]);
  return [...workspaceIds].filter(
    (workspaceId) =>
      previousSyncKeys.get(workspaceId) !== nextSyncKeys.get(workspaceId)
      || previousRefreshVersions.get(workspaceId)
        !== nextRefreshVersions.get(workspaceId),
  );
}
