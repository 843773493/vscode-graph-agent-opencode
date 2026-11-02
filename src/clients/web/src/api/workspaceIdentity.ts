import type { GatewayWorkspace } from "../types/backend";

export type GatewayWorkspaceRequestTarget = Pick<
  GatewayWorkspace,
  "workspace_id" | "backend_workspace_id" | "connection_kind"
>;

export type GatewayWorkspaceRoute = GatewayWorkspaceRequestTarget & {
  remote?: Pick<NonNullable<GatewayWorkspace["remote"]>, "gateway_id" | "remote_workspace_id"> | null;
};

export interface WorkspaceOwner {
  gatewayId: string;
  workspaceId: string;
}

export function gatewayWorkspaceOwner(workspace: GatewayWorkspaceRoute, localGatewayId: string): WorkspaceOwner {
  const gatewayId = workspace.connection_kind === "local" ? localGatewayId : workspace.remote?.gateway_id;
  const workspaceId = workspace.connection_kind === "local" ? workspace.backend_workspace_id : workspace.remote?.remote_workspace_id;
  if (!gatewayId?.trim() || !workspaceId?.trim()) throw new Error("已登记路由缺少目标 Gateway／工作区身份");
  return { gatewayId, workspaceId };
}

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

export function requireGatewayWorkspaceRoute(
  workspaces: readonly GatewayWorkspaceRoute[],
  localGatewayId: string | null | undefined,
  owner: WorkspaceOwner,
): GatewayWorkspaceRoute {
  if (!localGatewayId?.trim()) {
    throw new Error("Gateway 健康回执缺少身份，无法解析目标路由");
  }
  const { gatewayId, workspaceId } = owner;
  if (!gatewayId?.trim() || !workspaceId?.trim()) {
    throw new Error("资源目标缺少 Gateway／工作区身份");
  }
  const targets = workspaces.filter((workspace) => workspace.connection_kind === "local"
    ? gatewayId === localGatewayId && workspace.backend_workspace_id === workspaceId
    : workspace.remote?.gateway_id === gatewayId && workspace.remote.remote_workspace_id === workspaceId);
  const [target] = targets;
  if (!target || targets.length !== 1) {
    throw new Error("目标 Gateway／工作区缺少唯一的已登记路由，无法打开资源");
  }
  return target;
}
