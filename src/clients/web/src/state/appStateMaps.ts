import type { AppState } from "../types/frontend";

export function cloneMaps(state: AppState): AppState {
  return {
    ...state,
    eventQueuesBySession: new Map(state.eventQueuesBySession),
    sessionTraceHistoryBySession: new Map(state.sessionTraceHistoryBySession),
    pendingConversations: new Map(state.pendingConversations),
    activeJobIdsBySession: new Map(state.activeJobIdsBySession),
    unreadSessionKeys: new Set(state.unreadSessionKeys),
    gatewayUserViewStates: new Map(state.gatewayUserViewStates),
    sessionAttachmentSummaries: new Map(state.sessionAttachmentSummaries),
    sessionsByWorkspace: new Map(state.sessionsByWorkspace),
    sessionGatewayWorkspaceById: new Map(state.sessionGatewayWorkspaceById),
    turnTimelinesBySession: new Map(state.turnTimelinesBySession ?? []),
    messageStreamsByTurnStream: new Map(state.messageStreamsByTurnStream ?? []),
  };
}
