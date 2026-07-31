import type { Dispatch, SetStateAction } from "react";
import { getSession } from "../../api/sessions";
import { cloneMaps } from "../../state/appStateMaps";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { replaceSessionMetadata } from "../../state/session/sessions";
import type { AppState } from "../../types/frontend";
import {
  fetchWorkspaceSessionListSnapshot,
  isCurrentWorkspaceSessionListSnapshot,
} from "../workspaceSessionListRefresh";

export type SetAppState = Dispatch<SetStateAction<AppState>>;

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
): Promise<void> {
  const updatedSession = await getSession(apiPort, sessionId, workspaceId);
  setState((previous) => {
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
  workspaceId: string | null,
  setState: SetAppState,
): Promise<void> {
  if (!workspaceId) {
    throw new Error("刷新委派子会话时缺少 workspace_id");
  }
  const snapshot = await fetchWorkspaceSessionListSnapshot(apiPort, workspaceId);
  if (!isCurrentWorkspaceSessionListSnapshot(snapshot)) {
    return;
  }
  setState((previous) => {
    if (!isCurrentWorkspaceSessionListSnapshot(snapshot)) {
      return previous;
    }
    const previousWorkspaceSessions =
      previous.sessionsByWorkspace.get(workspaceId) ?? [];
    if (sessionListsMatch(previousWorkspaceSessions, snapshot.sessions)) {
      return previous;
    }
    const next = cloneMaps(previous);
    next.sessionsByWorkspace.set(workspaceId, snapshot.sessions);
    for (const session of snapshot.sessions) {
      next.sessionGatewayWorkspaceById.set(
        sessionScopeKey(workspaceId, session.session_id),
        workspaceId,
      );
    }
    if (
      previous.activeGatewayWorkspaceId === workspaceId
      || previous.currentSessionWorkspaceId === workspaceId
    ) {
      next.sessions = snapshot.sessions;
    }
    return next;
  });
}
