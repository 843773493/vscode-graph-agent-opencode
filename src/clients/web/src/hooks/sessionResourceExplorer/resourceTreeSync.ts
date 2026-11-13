import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import type { GatewayWorkspace } from "../../types/backend";

/** 旧批次已失去目录发布权；调用方必须保留原消费水位，等待最新 confirmed 对账。 */
export class CatalogReadSupersededError extends Error {}

export function buildWorkspaceNavigationSyncKey(
  workspaces: readonly GatewayWorkspace[],
): string {
  return workspaces
    .map((workspace) => `${gatewayWorkspaceKey(workspace)}\u0000${workspace.name}`)
    .sort()
    .join("\u0001");
}
