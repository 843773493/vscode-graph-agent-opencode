import type { Session } from "../types/backend";
import type { AppState } from "../types/frontend";
import { cloneMaps } from "./appStateMaps";

export function replaceSessionMetadata(
  state: AppState,
  updatedSession: Session,
): AppState {
  const next = cloneMaps(state);
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
  }
  return next;
}
