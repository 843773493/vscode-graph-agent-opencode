import type { AppState } from "../types/frontend";

export function cloneMaps(state: AppState): AppState {
  return {
    ...state,
    eventQueuesBySession: new Map(state.eventQueuesBySession),
    pendingConversations: new Map(state.pendingConversations),
    sessionAttachmentSummaries: new Map(state.sessionAttachmentSummaries),
    sessionsByWorkspace: new Map(state.sessionsByWorkspace),
    sessionGatewayWorkspaceById: new Map(state.sessionGatewayWorkspaceById),
  };
}
