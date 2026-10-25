import type { Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import { cloneMaps } from "../../../state/appStateMaps";
import { clearLastSessionId, writeLastSessionId } from "../../../state/storage";
import { applySessionListConvergence } from "../../../state/session/sessions";
import { appendFrontendEvent } from "../../../state/traceEvents";
import { resetAgentStateFields } from "../../runtime/useAgentStateSnapshot";
import { resetSessionScopedFields } from "../sessionScopedState";

export function normalizeSessionTitle(title: string): string {
  const trimmed = title.trim();
  if (!trimmed) {
    throw new Error("会话名称不能为空");
  }
  return trimmed;
}

export function currentSessionMatchesOwner(
  state: Pick<
    AppState,
    "currentSession" | "currentSessionWorkspaceId" | "activeGatewayWorkspaceId"
  >,
  owner: { sessionId: string | null; workspaceId: string | null },
): boolean {
  return (state.currentSession?.session_id ?? null) === owner.sessionId
    && (state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId)
      === owner.workspaceId;
}

/** 把一条会话登记为当前会话后的共同落点：复位会话级显示缓存、写入历史加载
 * 状态并追加一条 session_selected 事件。selectSession 与 selectWorkspaceSession
 * 原本逐字复制同一段 25 行，只差事件载荷；新增会话级字段时极易只改一处。 */
export function applySessionSelectionTransition(
  next: AppState,
  previous: AppState,
  selected: Session,
  cacheKey: string,
  eventPayload: Record<string, unknown>,
): void {
  next.unreadSessionKeys.delete(cacheKey);
  next.traceEvents = [];
  next.llmRequestLogs = [];
  next.llmRequestLogsLoadedAt = null;
  next.llmRequestLogsLoading = previous.contentView === "requests";
  next.llmRequestLogsError = null;
  next.sessionResources = [];
  next.sessionResourcesLoadedAt = null;
  next.sessionResourcesLoading = previous.contentView === "resources";
  next.sessionResourcesError = null;
  next.pendingConversations.delete(cacheKey);
  next.contentView = previous.contentView === "agent" ? "default" : previous.contentView;
  next.status = "正在加载会话历史";
  Object.assign(next, resetAgentStateFields(next));
  writeLastSessionId(selected.session_id);
  appendFrontendEvent(
    next.eventQueuesBySession,
    selected.session_id,
    "session_selected",
    "切换会话",
    eventPayload,
    selected.title,
    cacheKey,
  );
}

/** 把一条会话标记为已读的唯一出口。原本 selectSession 与 selectWorkspaceSession
 * 各写一份逐字相同的 setState 块，未读判定与写回必须只有一处。 */
export function clearUnreadSessionKey(state: AppState, cacheKey: string): AppState {
  if (!state.unreadSessionKeys.has(cacheKey)) return state;
  const next = cloneMaps(state);
  next.unreadSessionKeys.delete(cacheKey);
  return next;
}

/**
 * 本地已知的删除闭包：被删会话及其按 `parent_session_id` 可达的全部后代。
 *
 * 后端删除会级联整棵子会话树，本地镜像必须用同一闭包一次性收敛，否则被删子树
 * 会作为幽灵条目残留（甚至被选为新的当前会话）。这是删除成功路径的唯一收敛依据；
 * 不得再为此无条件重拉整个会话列表（§10.3 前端半边）。
 */
export function collectCascadedSessionIds(
  sessions: readonly Session[],
  rootSessionId: string,
): Set<string> {
  const childSessionIdsByParent = new Map<string, string[]>();
  for (const session of sessions) {
    const parentSessionId = session.parent_session_id;
    if (!parentSessionId) continue;
    const childSessionIds = childSessionIdsByParent.get(parentSessionId);
    if (childSessionIds) {
      childSessionIds.push(session.session_id);
    } else {
      childSessionIdsByParent.set(parentSessionId, [session.session_id]);
    }
  }

  const deletedSessionIds = new Set<string>([rootSessionId]);
  const pendingSessionIds = [rootSessionId];
  for (let cursor = 0; cursor < pendingSessionIds.length; cursor += 1) {
    const parentSessionId = pendingSessionIds[cursor]!;
    const childSessionIds = childSessionIdsByParent.get(parentSessionId);
    if (!childSessionIds) continue;
    for (const childSessionId of childSessionIds) {
      if (deletedSessionIds.has(childSessionId)) continue;
      deletedSessionIds.add(childSessionId);
      pendingSessionIds.push(childSessionId);
    }
  }
  return deletedSessionIds;
}

/**
 * 会话已消失后的统一收敛：按权威剩余列表收敛工作区镜像，并在当前会话确实已从
 * 剩余列表中消失（含被删父会话级联带走的后代）时清空或切到剩余首个会话。
 *
 * 删除成功路径与「请求报错但补偿重取成功」的失败路径必须共用这一份：失败路径若
 * 只收敛 sessions 镜像而不校正 currentSession，就会出现 currentSession 悬空指向
 * 一个已不在 sessions 里的幽灵会话（与成功路径行为不一致）。
 *
 * `reloadHistoryAfterSwitch` 只在「删除非当前会话、但级联带走了当前会话」时为真，
 * 让新选中的会话触发一次历史加载；删除当前会话时调用方已先行切走，无需重复。
 */
export function convergeSessionStateAfterRemoval(
  prev: AppState,
  workspaceId: string,
  remainingSessions: Session[],
  options: { reloadHistoryAfterSwitch: boolean; selectNextSession: boolean },
): AppState {
  const next = applySessionListConvergence(prev, workspaceId, remainingSessions);
  const remainingIds = new Set(
    remainingSessions.map((session) => session.session_id),
  );
  const currentWasDeleted = Boolean(
    prev.currentSession
    && (prev.currentSessionWorkspaceId ?? prev.activeGatewayWorkspaceId) === workspaceId
    && !remainingIds.has(prev.currentSession.session_id),
  );
  if (!currentWasDeleted) {
    return next;
  }
  const nextSession = options.selectNextSession ? remainingSessions[0] ?? null : null;
  next.currentSession = nextSession;
  next.currentSessionWorkspaceId = nextSession ? workspaceId : null;
  if (options.reloadHistoryAfterSwitch) {
    next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
  }
  Object.assign(next, resetSessionScopedFields(next));
  next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
  Object.assign(next, resetAgentStateFields(next));
  if (nextSession) {
    writeLastSessionId(nextSession.session_id);
  } else {
    clearLastSessionId();
  }
  return next;
}

