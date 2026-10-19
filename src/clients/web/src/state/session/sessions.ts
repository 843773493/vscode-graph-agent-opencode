import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { cloneMaps } from "../appStateMaps";
import { sessionScopeKey } from "./sessionScope";

/** 首页只能替换已返回的完整对象；缺席节点只有在完整列表中才表示已移除。 */
export function mergeSessionListPage(
  knownSessions: readonly Session[],
  page: readonly Session[],
  complete: boolean,
): Session[] {
  if (complete) return [...page];
  const returnedIds = new Set(page.map((session) => session.session_id));
  return [...page, ...knownSessions.filter((session) => !returnedIds.has(session.session_id))];
}

/** 异步补读只更新仍在会话镜像中的精确 owner，删除后的迟到结果不得复活会话。 */
export function hasSessionOwner(
  state: AppState,
  sessionId: string,
  workspaceId: string | null,
): boolean {
  if (state.currentSession?.session_id === sessionId
    && (state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId) === workspaceId) {
    return true;
  }
  const sessions = workspaceId
    ? state.sessionsByWorkspace.get(workspaceId)
      ?? (state.activeGatewayWorkspaceId === workspaceId ? state.sessions : [])
    : state.sessions;
  return sessions?.some((session) => session.session_id === sessionId) ?? false;
}

/** 权威列表收敛时，统一清除已消失 owner 的所有会话级缓存。 */
export function applySessionListConvergence(
  state: AppState,
  workspaceId: string,
  remainingSessions: Session[],
): AppState {
  const next = cloneMaps(state);
  const previousSessions = state.sessionsByWorkspace.get(workspaceId)
    ?? (state.activeGatewayWorkspaceId === workspaceId ? state.sessions : []);
  if (state.activeGatewayWorkspaceId === workspaceId) next.sessions = remainingSessions;
  next.sessionsByWorkspace.set(workspaceId, remainingSessions);
  const remainingIds = new Set(remainingSessions.map((session) => session.session_id));
  for (const removed of previousSessions) {
    if (remainingIds.has(removed.session_id)) continue;
    const cacheKey = sessionScopeKey(workspaceId, removed.session_id);
    next.sessionAttachmentSummaries.delete(cacheKey);
    next.eventQueuesBySession.delete(cacheKey);
    next.sessionTraceHistoryBySession.delete(cacheKey);
    next.pendingConversations.delete(cacheKey);
    next.activeJobIdsBySession.delete(cacheKey);
    next.unreadSessionKeys.delete(cacheKey);
    next.sessionGatewayWorkspaceById.delete(cacheKey);
    next.turnTimelinesBySession.delete(cacheKey);
    for (const [streamKey, stream] of next.messageStreamsByTurnStream ?? []) {
      if (stream.sessionCacheKey === cacheKey) next.messageStreamsByTurnStream?.delete(streamKey);
    }
  }
  return next;
}

export function replaceSessionMetadata(
  state: AppState,
  updatedSession: Session,
  workspaceIdOverride?: string | null,
): AppState {
  const next = cloneMaps(state);
  const workspaceId =
    workspaceIdOverride ?? state.activeGatewayWorkspaceId ?? updatedSession.workspace_id;
  if (workspaceId === state.activeGatewayWorkspaceId) {
    next.sessions = state.sessions.map((session) =>
      session.session_id === updatedSession.session_id ? updatedSession : session,
    );
    if (!next.sessions.some((session) => session.session_id === updatedSession.session_id)) {
      next.sessions = [updatedSession, ...next.sessions];
    }
  }
  if (state.currentSession?.session_id === updatedSession.session_id
    && (state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId) === workspaceId) {
    next.currentSession = updatedSession;
    next.currentSessionWorkspaceId = workspaceId;
  }
  next.sessionGatewayWorkspaceById.set(
    sessionScopeKey(workspaceId, updatedSession.session_id),
    workspaceId,
  );
  const workspaceSessions = next.sessionsByWorkspace.get(workspaceId) ?? [];
  const updatedWorkspaceSessions = workspaceSessions.map((session) =>
    session.session_id === updatedSession.session_id ? updatedSession : session,
  );
  if (
    !updatedWorkspaceSessions.some(
      (session) => session.session_id === updatedSession.session_id,
    )
  ) {
    updatedWorkspaceSessions.unshift(updatedSession);
  }
  next.sessionsByWorkspace.set(workspaceId, updatedWorkspaceSessions);
  return next;
}
