import type { Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import { getSession as apiGetSession } from "../../../api";
import { HttpRequestError } from "../../../api/http";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { replaceSessionMetadata } from "../../../state/session/sessions";
import type { SetAppState } from "../../contentViewLoaderTypes";
import { convergeSessionStateAfterRemoval } from "./sessionLifecycleTransitions";

/** 目录版本失效时只核对已打开 owner；deleting 的 DTO 仍可读取，404 才收敛删除。 */
export async function refreshCurrentSessionCatalogOwner(
  apiPort: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  readState: () => AppState,
  setState: SetAppState,
  signal: AbortSignal,
): Promise<void> {
  const before = readState();
  const sessionId = before.currentSession?.session_id;
  const workspaceId = workspaceTarget.workspace_id;
  if (!sessionId || (before.currentSessionWorkspaceId ?? before.activeGatewayWorkspaceId) !== workspaceId) {
    return;
  }
  const stillCurrent = (state: AppState) => !signal.aborted
    && state.currentSession?.session_id === sessionId
    && (state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId) === workspaceId
    && state.apiPort === before.apiPort
    && state.gatewayUserAccess?.kind === before.gatewayUserAccess?.kind
    && state.gatewayUserAccess?.user_id === before.gatewayUserAccess?.user_id;
  let updatedSession: Session | null = null;
  try {
    updatedSession = await apiGetSession(apiPort, sessionId, workspaceId, { signal });
    if (updatedSession.session_id !== sessionId
      || updatedSession.workspace_id !== workspaceTarget.backend_workspace_id) {
      throw new Error(`当前会话目录对账返回了其它 owner: ${workspaceId}/${sessionId}`);
    }
  } catch (error: unknown) {
    if (!(error instanceof HttpRequestError) || error.status !== 404) throw error;
  }
  setState((previous) => {
    if (!stillCurrent(previous)) return previous;
    if (updatedSession) return replaceSessionMetadata(previous, updatedSession, workspaceId);
    const sessions = previous.sessionsByWorkspace.get(workspaceId)
      ?? (previous.activeGatewayWorkspaceId === workspaceId ? previous.sessions : []);
    const next = convergeSessionStateAfterRemoval(previous, workspaceId,
      sessions.filter((session) => session.session_id !== sessionId),
      { reloadHistoryAfterSwitch: true, selectNextSession: false });
    next.status = `当前会话已删除: ${sessionId}`;
    return next;
  });
}
