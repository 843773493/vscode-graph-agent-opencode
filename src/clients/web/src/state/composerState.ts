import type { Agent, Session, SessionGoal, WebUiSettings } from "../types/backend";
import type { AppState, ConversationContentView } from "../types/frontend";

export interface ComposerStateSnapshot {
  apiPort: number | null;
  activeGatewayWorkspaceId: string | null;
  gatewayUserScope: string | null;
  uiSettings: WebUiSettings;
  agents: Agent[];
  currentSession: Session | null;
  currentSessionWorkspaceId: string | null;
  contentView: ConversationContentView;
  currentGoal: SessionGoal | null;
  currentGoalSessionId: string | null;
  goalLoading: boolean;
  goalError: string | null;
  compactLoading: boolean;
  currentActiveJobId: string | null;
  queuedPendingCount: number;
}

export function selectComposerState(
  state: AppState,
  sessionCacheKey: string | null,
): ComposerStateSnapshot {
  const pendingConversations = sessionCacheKey
    ? state.pendingConversations.get(sessionCacheKey) ?? []
    : [];
  return {
    apiPort: state.apiPort,
    activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    gatewayUserScope: state.gatewayUserAccess
      ? state.gatewayUserAccess.kind === "user" && state.gatewayUserAccess.user_id
        ? `user:${state.gatewayUserAccess.user_id}`
        : null
      : null,
    uiSettings: state.uiSettings,
    agents: state.agents,
    currentSession: state.currentSession,
    currentSessionWorkspaceId: state.currentSessionWorkspaceId,
    contentView: state.contentView,
    currentGoal: state.currentGoal,
    currentGoalSessionId: state.currentGoalSessionId,
    goalLoading: state.goalLoading,
    goalError: state.goalError,
    compactLoading: state.compactLoading,
    currentActiveJobId: sessionCacheKey
      ? state.activeJobIdsBySession.get(sessionCacheKey) ?? null
      : null,
    queuedPendingCount: pendingConversations.filter(
      (conversation) => conversation.pending && conversation.status === "queued",
    ).length,
  };
}

export function reuseComposerStateSnapshot(
  previous: ComposerStateSnapshot | null,
  next: ComposerStateSnapshot,
): ComposerStateSnapshot {
  if (!previous) {
    return next;
  }
  const keys = Object.keys(next) as Array<keyof ComposerStateSnapshot>;
  return keys.every((key) => Object.is(previous[key], next[key]))
    ? previous
    : next;
}
