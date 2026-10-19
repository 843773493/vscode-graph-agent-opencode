import { getSession } from "../../api/session/sessions";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { applySessionListConvergence, hasSessionOwner, mergeSessionListPage, replaceSessionMetadata } from "../../state/session/sessions";
import type { AppState } from "../../types/frontend";
import type { SetAppState } from "../contentViewLoaderTypes";
import {
  fetchWorkspaceSessionListSnapshot,
  isCurrentWorkspaceSessionListSnapshot,
} from "../workspace/workspaceSessionListRefresh";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";

function sessionListsMatch(
  left: AppState["sessions"],
  right: AppState["sessions"],
): boolean {
  return left.length === right.length && left.every((session, index) => {
    const candidate = right[index];
    return candidate !== undefined
      && session.session_id === candidate.session_id
      && session.updated_at === candidate.updated_at;
  });
}

export async function refreshSessionMetadata(
  apiPort: number,
  sessionId: string,
  workspaceId: string | null,
  sessionCacheKey: string,
  setState: SetAppState,
  announceAutoTitle: boolean = true,
  signal?: AbortSignal,
): Promise<void> {
  const updatedSession = await getSession(apiPort, sessionId, workspaceId, { signal });
  setState((previous) => {
    if (signal?.aborted || !hasSessionOwner(previous, sessionId, workspaceId)) return previous;
    if (workspaceId && previous.currentSessionWorkspaceId !== workspaceId) {
      return previous;
    }
    const next = replaceSessionMetadata(previous, updatedSession, workspaceId);
    next.currentSessionWorkspaceId = workspaceId ?? next.currentSessionWorkspaceId;
    if (
      announceAutoTitle
      && previous.currentSession?.session_id === updatedSession.session_id
    ) {
      next.status = `已自动命名会话: ${updatedSession.title}`;
    }
    if (workspaceId) {
      next.sessionGatewayWorkspaceById.set(sessionCacheKey, workspaceId);
    }
    return next;
  });
}

export async function refreshWorkspaceSessionList(
  apiPort: number,
  workspaceTarget: GatewayWorkspaceRequestTarget | null,
  setState: SetAppState,
  options: { force?: boolean } = {},
): Promise<void> {
  if (!workspaceTarget) {
    throw new Error("刷新工作区会话列表时缺少 workspace_id");
  }
  const workspaceId = workspaceTarget.workspace_id;
  const snapshot = await fetchWorkspaceSessionListSnapshot(
    apiPort,
    workspaceTarget,
    options,
  );
  if (!isCurrentWorkspaceSessionListSnapshot(snapshot)) {
    return;
  }
  setState((previous) => {
    if (!isCurrentWorkspaceSessionListSnapshot(snapshot)) {
      return previous;
    }
    const previousWorkspaceSessions =
      previous.sessionsByWorkspace.get(workspaceId) ?? [];
    const sessions = mergeSessionListPage(previousWorkspaceSessions, snapshot.sessions, snapshot.complete);
    const currentSessionIsInWorkspace =
      previous.activeGatewayWorkspaceId === workspaceId
      || previous.currentSessionWorkspaceId === workspaceId;
    const currentSessionId = previous.currentSession?.session_id;
    const currentSessionRemoved = Boolean(
      currentSessionIsInWorkspace
      && currentSessionId
      && !sessions.some((session) => session.session_id === currentSessionId),
    );
    if (
      sessionListsMatch(previousWorkspaceSessions, sessions)
      && !currentSessionRemoved
    ) {
      return previous;
    }
    const next = applySessionListConvergence(previous, workspaceId, sessions);
    for (const session of sessions) {
      next.sessionGatewayWorkspaceById.set(
        sessionScopeKey(workspaceId, session.session_id),
        workspaceId,
      );
    }
    if (currentSessionIsInWorkspace) {
      if (currentSessionRemoved) {
        const nextSession = sessions[0] ?? null;
        next.currentSession = nextSession;
        next.currentSessionWorkspaceId = nextSession ? workspaceId : null;
      }
    }
    return next;
  });
}
