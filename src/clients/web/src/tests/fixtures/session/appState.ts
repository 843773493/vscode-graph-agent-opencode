import type { Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import type { SetAppState } from "../../../hooks/contentViewLoaderTypes";
/** 把最新 AppState 镜像到闭包，并把 setState 桥接成委托更新。 */
export function createAppStateMirror(initial: AppState): {
  readonly setState: SetAppState;
  current: () => AppState;
} {
  let current = initial;
  return {
    setState: (update) => {
      current = typeof update === "function"
        ? (update as (prev: AppState) => AppState)(current)
        : update;
    },
    current: () => current,
  };
}

interface SessionHookStateOverrides {
  workspaceId: string;
  currentSession: Session | null;
  sessions: Session[];
  gatewayWorkspaces?: { workspace_id: string; name: string }[];
  sessionsByWorkspace?: Map<string, Session[]>;
  sessionGatewayWorkspaceById?: Map<string, string>;
  eventQueuesBySession?: Map<string, unknown>;
  pendingConversations?: Map<string, unknown>;
  activeJobIdsBySession?: Map<string, string>;
  unreadSessionKeys?: Set<string>;
  sessionAttachmentSummaries?: Map<string, unknown>;
}

/**
 * 生命周期动作用例的 AppState 装配：会话列表与当前会话是最常覆盖的字段，
 * 其余会话级镜像统一给空值。只用于测试镜像，AppState 大部分字段与断言无关。
 */
export function createSessionHookState(
  overrides: SessionHookStateOverrides,
): AppState {
  const {
    workspaceId,
    currentSession,
    sessions,
    gatewayWorkspaces = [],
    sessionsByWorkspace = new Map([[workspaceId, sessions]]),
    sessionGatewayWorkspaceById = new Map(),
    eventQueuesBySession = new Map(),
    pendingConversations = new Map(),
    activeJobIdsBySession = new Map(),
    unreadSessionKeys = new Set(),
    sessionAttachmentSummaries = new Map(),
  } = overrides;
  return {
    gatewayWorkspaces,
    sessions,
    sessionsByWorkspace,
    sessionGatewayWorkspaceById,
    sessionAttachmentSummaries,
    eventQueuesBySession,
    pendingConversations,
    activeJobIdsBySession,
    unreadSessionKeys,
    activeGatewayWorkspaceId: workspaceId,
    currentSession,
    currentSessionWorkspaceId: workspaceId,
    contentView: "default",
    sessionHistoryReloadNonce: 0,
    status: "",
  } as unknown as AppState;
}
