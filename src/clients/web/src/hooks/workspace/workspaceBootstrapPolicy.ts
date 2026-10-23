import {
  HttpRequestError,
  isTransientNetworkError,
} from "../../api/http";
import { isNavigationBackpressureStatus } from "../../api/session/sessionCatalogOperations";
import type { getLatestGatewayUserViewState } from "../../api/gateway/userViewState";
import type { listGatewayWorkspaces } from "../../gatewayApi";

export type BootstrapSelection = { workspaceId: string; sessionId: string };

export type GatewayWorkspaceList = Awaited<ReturnType<typeof listGatewayWorkspaces>>;

export function isRetryableWorkspaceBootstrapError(error: unknown): boolean {
  // 只重试「明确可恢复」的错误：Gateway 代理上游尚未就绪的 502/503/504 背压，
  // 以及 fetch 网络层瞬时故障（TypeError / AbortError / 连接被拒等）。
  // 不能再用 error instanceof TypeError：它把 fetch 传输失败和业务/程序代码
  // 抛出的 TypeError（例如读 undefined 属性）一并吞进有界重试，属按错误类型
  // 的过宽匹配；统一收敛到 isTransientNetworkError 这一既有唯一判据。
  return (
    error instanceof HttpRequestError
    && isNavigationBackpressureStatus(error.status)
  ) || isTransientNetworkError(error);
}

export function selectHealthyGatewayWorkspace(
  workspaceList: GatewayWorkspaceList,
): string | null {
  let firstReadyWorkspaceId: string | null = null;
  let defaultReadyWorkspaceId: string | null = null;
  for (const workspace of workspaceList.items) {
    if (workspace.status !== "ready") continue;
    if (workspace.workspace_id === workspaceList.active_workspace_id) {
      return workspace.workspace_id;
    }
    firstReadyWorkspaceId ??= workspace.workspace_id;
    if (workspace.system_default) {
      defaultReadyWorkspaceId ??= workspace.workspace_id;
    }
  }
  return defaultReadyWorkspaceId ?? firstReadyWorkspaceId;
}

export function selectBootstrapSessionId({
  preferredSessionId,
  persistedSessionId,
  previousSessionId,
  userChanged,
}: {
  preferredSessionId?: string | null;
  persistedSessionId?: string | null;
  previousSessionId?: string | null;
  userChanged: boolean;
}): string | null {
  if (!userChanged && preferredSessionId) return preferredSessionId;
  if (persistedSessionId) return persistedSessionId;
  return userChanged ? null : (previousSessionId ?? null);
}

export function selectBootstrapToolDetailsExpanded({
  persistedToolDetailsExpanded,
  previousToolDetailsExpanded,
  userChanged,
}: {
  persistedToolDetailsExpanded?: boolean | null;
  previousToolDetailsExpanded: boolean;
  userChanged: boolean;
}): boolean {
  return userChanged
    ? persistedToolDetailsExpanded ?? false
    : previousToolDetailsExpanded;
}

export function canAcceptUserViewStateResponse(
  currentUserId: string | null | undefined,
  responseUserId: string,
): boolean {
  return typeof currentUserId === "string" && currentUserId === responseUserId;
}

export function canAcceptUserViewStateMutation({
  currentUserId,
  responseUserId,
  currentLeaseGeneration,
  requestLeaseGeneration,
}: {
  currentUserId: string | null | undefined;
  responseUserId: string;
  currentLeaseGeneration: number | null | undefined;
  requestLeaseGeneration: number;
}): boolean {
  return (
    canAcceptUserViewStateResponse(currentUserId, responseUserId)
    && currentLeaseGeneration === requestLeaseGeneration
  );
}

export function shouldRestorePersistedWorkspace({
  restorePersistedWorkspace,
  userViewState,
  activeWorkspaceId,
  availableWorkspaceIds,
}: {
  restorePersistedWorkspace: boolean;
  userViewState: Awaited<ReturnType<typeof getLatestGatewayUserViewState>>;
  activeWorkspaceId: string | null;
  availableWorkspaceIds: readonly string[];
}): boolean {
  return (
    restorePersistedWorkspace
    && userViewState !== null
    && availableWorkspaceIds.includes(userViewState.workspace_id)
    && activeWorkspaceId !== userViewState.workspace_id
  );
}
