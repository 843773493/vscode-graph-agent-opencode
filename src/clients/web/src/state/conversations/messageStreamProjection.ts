import type { TurnResponsePart } from "../../types/backend";
import type { AppState, ConversationView } from "../../types/frontend";
import {
  messageStreamToResponseParts,
  type MessageStreamState,
} from "../messageStream/index";
import { isTerminalStatus } from "../messageStream/state";
import { TERMINAL_TURN_STATUSES } from "./conversationMerge";

const ACTIVITY_PART_KINDS = new Set([
  "reasoning",
  "reasoning_summary",
  "reasoning_encrypted",
  "tool_call",
  "tool_result",
]);

function messageStreamActivityStats(
  stream: MessageStreamState,
  parts: TurnResponsePart[],
  turnStartedAt?: string,
): NonNullable<ConversationView["activityStats"]> {
  // live 展示计时从 turnStartedAt 到已收到 block/tool execution 的最晚时间，
  // 单位为毫秒，负差为 0、任一端缺失/非法为 null；历史 DTO 使用后端投影的起止时间。
  let latestAt = Number.NEGATIVE_INFINITY;
  const includeTimestamp = (value: string | undefined) => {
    if (value === undefined) return;
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp) && timestamp > latestAt) latestAt = timestamp;
  };
  for (const block of stream.blocks) {
    includeTimestamp(block.started_at);
    includeTimestamp(block.updated_at);
    includeTimestamp(block.completed_at);
  }
  for (const execution of stream.toolExecutions) {
    includeTimestamp(execution.started_at);
    includeTimestamp(execution.updated_at);
    includeTimestamp(execution.completed_at);
  }
  const startedAt = turnStartedAt ? Date.parse(turnStartedAt) : Number.NaN;
  let itemCount = 0;
  for (const part of parts) {
    if (ACTIVITY_PART_KINDS.has(part.kind)) itemCount += 1;
  }
  return {
    duration_ms: Number.isFinite(startedAt) && Number.isFinite(latestAt)
      ? Math.max(0, latestAt - startedAt)
      : null,
    item_count: itemCount,
  };
}

function compareMessageStreamCandidates(
  left: MessageStreamState,
  right: MessageStreamState,
): number {
  return Number(isTerminalStatus(right.streamStatus))
    - Number(isTerminalStatus(left.streamStatus))
    || right.lastEventSeq - left.lastEventSeq
    || Number(right.connectionStatus === "terminal")
      - Number(left.connectionStatus === "terminal");
}

function preferredStreamsBySession(
  streams: Map<string, MessageStreamState>,
  sessionCacheKey: string,
): Map<string, Map<string, Map<string, MessageStreamState>>> {
  const bySession = new Map<string, Map<string, Map<string, MessageStreamState>>>();
  for (const candidate of streams.values()) {
    if (candidate.sessionCacheKey !== sessionCacheKey) continue;
    let byThread = bySession.get(candidate.sessionId);
    if (!byThread) {
      byThread = new Map();
      bySession.set(candidate.sessionId, byThread);
    }
    let byTurn = byThread.get(candidate.threadId);
    if (!byTurn) {
      byTurn = new Map();
      byThread.set(candidate.threadId, byTurn);
    }
    const current = byTurn.get(candidate.turnId);
    if (!current || compareMessageStreamCandidates(candidate, current) < 0) {
      byTurn.set(candidate.turnId, candidate);
    }
  }
  return bySession;
}

function terminalActivityStatsError(
  stream: MessageStreamState,
  conversation: ConversationView,
  liveItemCount: number,
): string | null {
  if (
    conversation.displayMode !== "history"
    || !isTerminalStatus(stream.streamStatus)
    || conversation.activityStats === undefined
    || conversation.activityStats.item_count === liveItemCount
  ) {
    return stream.protocolError;
  }
  const mismatch = `Turn Item 统计不一致: live=${liveItemCount} history=${conversation.activityStats.item_count}`;
  return stream.protocolError ? `${stream.protocolError}; ${mismatch}` : mismatch;
}

export function applyMessageStreamProjection(
  conversations: ConversationView[],
  streams: Map<string, MessageStreamState>,
  sessionCacheKey: string,
): ConversationView[] {
  const streamsBySession = preferredStreamsBySession(streams, sessionCacheKey);
  return conversations.map((conversation) => {
    if (conversation.threadId === undefined || conversation.turnId === undefined) {
      return conversation;
    }
    const stream = streamsBySession.get(conversation.sessionId)
      ?.get(conversation.threadId)
      ?.get(conversation.turnId);
    if (!stream) return conversation;
    const terminalTurn = conversation.turnStatus
      && TERMINAL_TURN_STATUSES.has(conversation.turnStatus);
    const liveResponseParts = messageStreamToResponseParts(stream);
    const liveActivityStats = messageStreamActivityStats(
      stream,
      liveResponseParts,
      conversation.userMessage?.created_at,
    );
    if (terminalTurn) {
      // Job API/Turn projection 已经确认终态时，任何旧 stream（包括错误的
      // completed）只能作为诊断镜像保留，不能重新驱动聊天状态或活动遮罩。
      const terminalConversationStatus =
        conversation.turnStatus === "completed"
        || conversation.turnStatus === "succeeded"
          ? "done"
          : "error";
      return {
        ...conversation,
        ...(conversation.displayMode === "live"
          && isTerminalStatus(stream.streamStatus)
          ? {
              responseParts: liveResponseParts,
              activityStats: liveActivityStats,
            }
          : {}),
        status: terminalConversationStatus,
        activeJobOverlay: false,
        pending: false,
        messageStream: {
          connectionStatus: stream.connectionStatus,
          streamStatus: stream.streamStatus,
          lastEventSeq: stream.lastEventSeq,
          failure: stream.failure,
          protocolError: terminalActivityStatsError(
            stream,
            conversation,
            liveActivityStats.item_count,
          ),
          activeState: stream.activeState,
          activities: stream.activities,
          resumable: stream.resumable,
        },
      };
    }
    const responseParts = liveResponseParts;
    const terminalStatus = stream.streamStatus === "completed"
      ? "done"
      : stream.streamStatus === "interrupted" || stream.streamStatus === "failed"
        ? "error"
        : "running";
    return {
      ...conversation,
      responseParts,
      activityStats: liveActivityStats,
      status: terminalStatus,
      activeJobOverlay: !isTerminalStatus(stream.streamStatus),
      messageStream: {
        connectionStatus: stream.connectionStatus,
        streamStatus: stream.streamStatus,
        lastEventSeq: stream.lastEventSeq,
        failure: stream.failure,
        protocolError: stream.protocolError,
        activeState: stream.activeState,
        activities: stream.activities,
        resumable: stream.resumable,
      },
    };
  });
}

/**
 * 返回当前会话仍需消费的消息流 Turn。
 *
 * Job/Trace 终态可能先于 message.v1 的最后几帧到达。此时业务层会清除
 * activeJobIdsBySession，但消息流仍是当前 live 会话的展示来源，不能因为
 * Job 已结束就立刻卸载 SSE。
 */
export function messageStreamTurnIdForSession(
  sessionId: string,
  state: AppState,
  sessionCacheKey: string = sessionId,
): string | null {
  const activeJobId = state.activeJobIdsBySession.get(sessionCacheKey);
  const pending = state.pendingConversations.get(sessionCacheKey) ?? [];
  const timeline = state.turnTimelinesBySession?.get(sessionCacheKey);
  if (activeJobId) {
    const activePending = pending.find((conversation) => conversation.jobId === activeJobId
      && conversation.threadId === (timeline?.threadId ?? state.currentSession?.thread_id));
    if (activePending?.turnId) return activePending.turnId;
    if (timeline) {
      for (const turnKey in timeline.turnsById) {
        if (!Object.prototype.hasOwnProperty.call(timeline.turnsById, turnKey)) continue;
        const turn = timeline.turnsById[turnKey];
        if (turn.job_id === activeJobId) return turn.turn_id;
      }
    }
    return null;
  }

  const pendingTurnsByThread = new Map<string, Set<string>>();
  for (const conversation of pending) {
    if (conversation.threadId === undefined || conversation.turnId === undefined) continue;
    let turnIds = pendingTurnsByThread.get(conversation.threadId);
    if (!turnIds) {
      turnIds = new Set();
      pendingTurnsByThread.set(conversation.threadId, turnIds);
    }
    turnIds.add(conversation.turnId);
  }

  let candidate: MessageStreamState | undefined;
  for (const stream of state.messageStreamsByTurnStream?.values() ?? []) {
    if (
      stream.sessionCacheKey !== sessionCacheKey
      || stream.sessionId !== sessionId
      || !pendingTurnsByThread.get(stream.threadId)?.has(stream.turnId)
      || isTerminalStatus(stream.streamStatus)
    ) {
      continue;
    }
    if (!candidate || stream.lastEventSeq > candidate.lastEventSeq) candidate = stream;
  }
  return candidate?.turnId ?? null;
}
