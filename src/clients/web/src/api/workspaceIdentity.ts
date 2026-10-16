import type { GatewayWorkspace, SessionForkResult } from "../types/backend";

export type GatewayWorkspaceRequestTarget = Pick<
  GatewayWorkspace,
  "workspace_id" | "backend_workspace_id" | "connection_kind"
>;

export type GatewayWorkspaceForkRoute = GatewayWorkspaceRequestTarget & {
  remote?: Pick<NonNullable<GatewayWorkspace["remote"]>, "gateway_id" | "remote_workspace_id"> | null;
};

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

export function requireForkTargetWorkspace(
  workspaces: readonly GatewayWorkspaceForkRoute[],
  localGatewayId: string | null | undefined,
  result: Pick<SessionForkResult, "thread_uri" | "thread_id"> & {
    session: Pick<SessionForkResult["session"], "workspace_id" | "session_id">;
  },
): GatewayWorkspaceRequestTarget {
  if (!localGatewayId?.trim()) {
    throw new Error("分支已创建，但 Gateway 健康回执缺少身份，无法解析目标路由");
  }
  const address = /^boxteam:\/\/([^\s/\\%?#]+)\/workspace\/([^\s/\\%?#]+)\/resources\/session\/([^\s/\\%?#]+)\/threads\/([^\s/\\%?#]+)$/.exec(result.thread_uri);
  if (!address) {
    throw new Error("分支已创建，但目标 Thread URI 缺少完整 Gateway／工作区身份");
  }
  const [, gatewayId, workspaceId, sessionId, threadId] = address;
  if (workspaceId !== result.session.workspace_id || sessionId !== result.session.session_id || threadId !== result.thread_id) {
    throw new Error("分支已创建，但目标 Thread URI 与发布回执中的会话／线程不一致");
  }
  const targets = workspaces.filter((workspace) => workspace.connection_kind === "local"
    ? gatewayId === localGatewayId && workspace.backend_workspace_id === workspaceId
    : workspace.remote?.gateway_id === gatewayId && workspace.remote.remote_workspace_id === workspaceId);
  const [target] = targets;
  if (!target || targets.length !== 1) {
    throw new Error("分支已创建，但目标 Gateway／工作区缺少唯一的已登记路由，无法打开目标线程");
  }
  return target;
}
