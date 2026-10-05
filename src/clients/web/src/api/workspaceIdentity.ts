import type { GatewayWorkspace } from "../types/backend";

export type GatewayWorkspaceRequestTarget = Pick<
  GatewayWorkspace,
  "workspace_id" | "backend_workspace_id" | "connection_kind"
>;

export function requireGatewayWorkspaceRequestTarget(
  workspaces: readonly GatewayWorkspaceRequestTarget[],
  gatewayWorkspaceId: string,
): GatewayWorkspaceRequestTarget {
  const target = workspaces.find(
    (workspace) => workspace.workspace_id === gatewayWorkspaceId,
  );
  if (!target) {
    throw new Error(
      `Gateway workspace target 不存在，无法解析 backend_workspace_id: ${gatewayWorkspaceId}`,
    );
  }
  if (target.connection_kind === "local" && !target.backend_workspace_id) {
    throw new Error(
      `Gateway workspace target 缺少 backend_workspace_id: ${gatewayWorkspaceId}`,
    );
  }
  return target;
}
