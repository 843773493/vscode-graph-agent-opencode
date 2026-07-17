import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { cloneMaps } from "../appStateMaps";
import { sessionScopeKey } from "./sessionScope";

export function replaceSessionMetadata(
  state: AppState,
  updatedSession: Session,
  workspaceIdOverride?: string | null,
): AppState {
  const next = cloneMaps(state);
  const workspaceId =
    workspaceIdOverride ?? state.activeGatewayWorkspaceId ?? updatedSession.workspace_id;
  next.sessions = state.sessions.map((session) =>
    session.session_id === updatedSession.session_id ? updatedSession : session,
  );
  if (
    !next.sessions.some(
      (session) => session.session_id === updatedSession.session_id,
    )
  ) {
    next.sessions = [updatedSession, ...next.sessions];
  }
  if (state.currentSession?.session_id === updatedSession.session_id) {
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
