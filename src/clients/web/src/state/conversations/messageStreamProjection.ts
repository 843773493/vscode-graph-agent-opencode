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
  const timestamps = [
    ...stream.blocks.flatMap((block) => [
      block.started_at,
      block.updated_at,
      block.completed_at,
    ]),
    ...stream.toolExecutions.flatMap((execution) => [
      execution.started_at,
      execution.updated_at,
      execution.completed_at,
    ]),
  ].filter((value): value is string => typeof value === "string");
  const startedAt = turnStartedAt ? Date.parse(turnStartedAt) : Number.NaN;
  const latestAt = Math.max(
    ...timestamps
      .map((value) => Date.parse(value))
      .filter((value) => Number.isFinite(value)),
  );
  return {
    duration_ms: Number.isFinite(startedAt) && Number.isFinite(latestAt)
      ? Math.max(0, latestAt - startedAt)
      : null,
    item_count: parts.filter((part) => ACTIVITY_PART_KINDS.has(part.kind)).length,
  };
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
): ConversationView[] {
  return conversations.map((conversation) => {
    const streamCandidates = [...streams.values()].filter((candidate) =>
      candidate.sessionId === conversation.sessionId
      && candidate.turnId === (conversation.turnId ?? conversation.jobId),
    );
    const stream = streamCandidates.sort((left, right) =>
      Number(isTerminalStatus(right.streamStatus))
        - Number(isTerminalStatus(left.streamStatus))
      || right.lastEventSeq - left.lastEventSeq
      || Number(right.connectionStatus === "terminal")
        - Number(left.connectionStatus === "terminal"),
    )[0];
    if (!stream) return conversation;
    if (
      conversation.turnId !== stream.turnId
      && conversation.jobId !== stream.turnId
    ) {
      return conversation;
    }
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
  if (activeJobId) return activeJobId;

  const pendingJobIds = new Set(
    (state.pendingConversations.get(sessionCacheKey) ?? [])
      .map((conversation) => conversation.jobId)
      .filter((jobId): jobId is string => Boolean(jobId)),
  );
  const candidate = [...(state.messageStreamsByTurnStream ?? new Map()).values()]
    .filter((stream) =>
      stream.sessionId === sessionId
      && pendingJobIds.has(stream.turnId)
      && !["completed", "interrupted", "failed"].includes(stream.streamStatus),
    )
    .sort((left, right) => right.lastEventSeq - left.lastEventSeq)[0];
  return candidate?.turnId ?? null;
}
