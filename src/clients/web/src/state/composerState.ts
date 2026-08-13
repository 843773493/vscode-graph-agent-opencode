import type { Agent, GatewayWorkspace, Session, SessionGoal, WebUiSettings } from "../types/backend";
import type { AppState, ConversationContentView } from "../types/frontend";

export interface ComposerStateSnapshot {
  apiPort: number | null;
  gatewayWorkspaces: GatewayWorkspace[];
  activeGatewayWorkspaceId: string | null;
  workspaceSwitching: boolean;
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
  hasCurrentSessionHistory: boolean;
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
    gatewayWorkspaces: state.gatewayWorkspaces,
    activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    workspaceSwitching: state.workspaceSwitching,
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
    // 已选中的会话始终按会话模式展示，不能等待历史内容判断，避免长历史切换时闪回新会话选择器。
    hasCurrentSessionHistory: state.currentSession !== null,
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
