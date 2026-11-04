import type { GatewayWorkspace } from "../types/backend";

export type GatewayWorkspaceRequestTarget = Pick<GatewayWorkspace, "gateway_id" | "workspace_id">;

export interface WorkspaceOwner {
  gatewayId: string;
  workspaceId: string;
}

/** 仅供展示选择和内存 Map 使用；线上字段始终是原生 Gateway ID 与 UUID。 */
export function gatewayWorkspaceKey(workspace: GatewayWorkspaceRequestTarget | WorkspaceOwner): string {
  const owner = "gateway_id" in workspace ? gatewayWorkspaceOwner(workspace) : workspace;
  if (!owner.gatewayId?.trim() || !owner.workspaceId?.trim()) throw new Error("工作区缺少完整 Gateway／UUID 归属");
  return JSON.stringify([owner.gatewayId, owner.workspaceId]);
}

export function nullableGatewayWorkspaceKey(workspace: GatewayWorkspaceRequestTarget | null | undefined): string | null {
  return workspace ? gatewayWorkspaceKey(workspace) : null;
}

export function workspaceOwnerFromKey(key: string): WorkspaceOwner {
  // TODO：旧 selection／IndexedDB／outbox 的别名恢复需显式迁移；不能借当前 active 补归属。
  const value: unknown = JSON.parse(key);
  if (!Array.isArray(value) || value.length !== 2 || !value.every(item => typeof item === "string" && item.trim())) {
    throw new Error("工作区选择缺少完整 Gateway／UUID 归属，请从工作区导航重新选择");
  }
  return { gatewayId: value[0] as string, workspaceId: value[1] as string };
}

export function gatewayWorkspaceOwner(workspace: GatewayWorkspaceRequestTarget): WorkspaceOwner {
  if (!workspace.gateway_id?.trim() || !workspace.workspace_id?.trim()) throw new Error("已登记工作区缺少 Gateway／UUID 归属");
  return { gatewayId: workspace.gateway_id, workspaceId: workspace.workspace_id };
}

export function requireGatewayWorkspaceRequestTarget<T extends GatewayWorkspaceRequestTarget>(
  workspaces: readonly T[], selection: string | WorkspaceOwner,
): T {
  const owner = typeof selection === "string" ? workspaceOwnerFromKey(selection) : selection;
  const targets = workspaces.filter(workspace => workspace.gateway_id === owner.gatewayId && workspace.workspace_id === owner.workspaceId);
  const [target] = targets;
  if (!target || targets.length !== 1) throw new Error("目标 Gateway／UUID 缺少唯一已登记工作区");
  return target;
}
