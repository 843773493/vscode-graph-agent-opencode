import type {
  JobDispatchSnapshot,
  JobStatus,
  Message,
  PendingRequestList,
  TraceEvent,
  TurnResponsePart,
} from "../types/backend";
import type { AppState, ConversationView } from "../types/frontend";
import {
  messageStreamToResponseParts,
  type MessageStreamState,
} from "./messageStream/index";
import { isTerminalStatus } from "./messageStream/state";
import {
  conversationsMatch,
  dedupeConversationViews,
  mergeConversation,
  sortConversationViews,
  TERMINAL_TURN_STATUSES,
} from "./conversations/conversationMerge";
import { turnTimelineConversations } from "./conversations/turnProjection";
import {
  appendTraceEventsToPendingConversations,
  compactPendingConversationEvents,
  conversationMatchesTraceEvent,
  hasJobTerminalTraceEvent,
  PENDING_CONVERSATION_EVENT_LIMIT,
  statusForConversationEvents,
  traceEventsForConversation,
} from "./conversations/traceProjection";
import { writePendingList } from "./conversations/pendingMap";
import {
  dedupeTraceEvents,
  isJobTerminalTraceType,
} from "./traceEvents";

export {
  appendTraceEventsToPendingConversations,
  hasJobTerminalTraceEvent,
  PENDING_CONVERSATION_EVENT_LIMIT,
  sortConversationViews,
  statusForConversationEvents,
  traceEventsForConversation,
  writePendingList,
};

/**
 * 后端 JobDispatchSnapshot 直投到 ConversationView 的队列事实字段。
 *
 * 成功派发后前端必须以后端返回的完整对象替换本地状态，这里只做一次整体投影，
 * 不再逐字段手挑；后端未提供的可选值一律保持 undefined，不回退到请求参数或
 * 任何本地默认值，避免向前端伪造后端并不存在的队列事实。
 */
export function dispatchToConversationProjection(
  dispatch: JobDispatchSnapshot,
): Pick<
  ConversationView,
  | "deliveryPolicy"
  | "enqueueSequence"
  | "pendingPosition"
  | "queueSnapshotVersion"
  | "queuedJobCount"
  | "pendingJobCount"
  | "blockedByJobId"
> {
  return {
    deliveryPolicy: dispatch.delivery_policy ?? undefined,
    enqueueSequence: dispatch.enqueue_sequence ?? undefined,
    pendingPosition: dispatch.queued_jobs_ahead,
    queueSnapshotVersion: dispatch.queue_snapshot_version,
    queuedJobCount: dispatch.queued_job_count,
    pendingJobCount: dispatch.pending_job_count,
    blockedByJobId: dispatch.blocked_by_job_id,
  };
}

/** 把后端 dispatch 的活动 Job 写回会话级运行态镜像；dispatch 未给出活动 Job 时保持现状。 */
export function writeDispatchActiveJob(
  activeJobMap: Map<string, string>,
  dispatch: JobDispatchSnapshot,
  mapKey: string,
): void {
  if (dispatch.active_job_id) {
    activeJobMap.set(mapKey, dispatch.active_job_id);
  }
}

export function writePendingSnapshot(
  pendingMap: Map<string, ConversationView[]>,
  activeJobMap: Map<string, string>,
  snapshot: PendingRequestList,
  mapKey: string = snapshot.session_id,
) {
  const existingPending = pendingMap.get(mapKey) ?? [];
  const existingSnapshotVersion = Math.max(
    0,
    ...existingPending.map(
      (conversation) => conversation.queueSnapshotVersion ?? 0,
    ),
  );
  if ((snapshot.snapshot_version ?? 0) < existingSnapshotVersion) {
    return;
  }
  const existingActiveConversation = snapshot.active_job_id
    ? existingPending.find(
        (conversation) => conversation.jobId === snapshot.active_job_id,
      )
    : undefined;
  const snapshotConversations = pendingSnapshotToConversations(snapshot);
  if (
    snapshot.active_job_id
    && !snapshotConversations.some(
      (conversation) => conversation.jobId === snapshot.active_job_id,
    )
  ) {
    snapshotConversations.push(
      existingActiveConversation
        ? {
            ...existingActiveConversation,
            pending: true,
            pendingPosition: undefined,
            deliveryPolicy: undefined,
            activeJobOverlay: true,
          }
        : createActiveJobOverlay(snapshot.session_id, snapshot.active_job_id),
    );
  }
  // replay 的新 Job 可能尚未进入 bootstrap/pending-requests 快照，但后端已经
  // 移除了旧上下文。保留乐观 replay，避免上下文切换期间出现空聊天区。
  const optimisticReplayConversations = existingPending.filter(
    (conversation) =>
      conversation.pending
      && conversation.source === "pending"
      && Boolean(conversation.jobId)
      && conversation.userMessage?.metadata?.source === "optimistic_replay",
  );
  // 终态回合仍由实时消息流或 Trace 提供当前视图；下一次 bootstrap 会用
  // canonical 历史替换它，不能被空 pending 快照提前删掉。
  const terminalConversations = existingPending.filter(
    (conversation) =>
      conversation.source === "pending"
      && Boolean(conversation.jobId)
      && (
        hasJobTerminalTraceEvent(conversation.events)
        || ["completed", "succeeded", "failed", "cancelled", "timed_out"].includes(
          conversation.turnStatus ?? "",
        )
      ),
  );
  for (const conversation of [
    ...optimisticReplayConversations,
    ...terminalConversations,
  ]) {
    if (!snapshotConversations.some((candidate) => conversationsMatch(candidate, conversation))) {
      snapshotConversations.push(conversation);
    }
  }
  writePendingList(
    pendingMap,
    snapshot.session_id,
    snapshotConversations,
    mapKey,
  );
  if (snapshot.active_job_id) {
    activeJobMap.set(mapKey, snapshot.active_job_id);
  } else {
    activeJobMap.delete(mapKey);
  }
}

function createActiveJobOverlay(
  sessionId: string,
  jobId: string,
): ConversationView {
  return {
    conversationId: `active-job:${jobId}`,
    displayMode: "live",
    sessionId,
    userMessage: null,
    assistantMessages: [],
    events: [],
    status: "running",
    jobId,
    pending: true,
    pendingPosition: 0,
    activeJobOverlay: true,
    source: "pending",
  };
}

export function syncActiveJobConversation(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  activeJobId: string | null,
  mapKey: string = sessionId,
): void {
  const existing = map.get(mapKey) ?? [];
  const retained = existing.filter(
    (conversation) =>
      !conversation.activeJobOverlay || conversation.jobId === activeJobId,
  ).map((conversation) => conversation.jobId === activeJobId
    ? {
        ...conversation,
        pending: true,
        pendingPosition: undefined,
        deliveryPolicy: undefined,
        activeJobOverlay: true,
      }
    : conversation);
  if (
    activeJobId
    && !retained.some((conversation) => conversation.jobId === activeJobId)
  ) {
    retained.push(createActiveJobOverlay(sessionId, activeJobId));
  }
  writePendingList(map, sessionId, retained, mapKey);
}

export function pendingSnapshotToConversations(
  snapshot: PendingRequestList,
): ConversationView[] {
  return [...(snapshot.requests ?? [])]
    .sort((left, right) => left.enqueue_sequence - right.enqueue_sequence)
    .map((request) => ({
    conversationId: request.message_id,
    displayMode: "live" as const,
    sessionId: request.session_id,
    userMessage: {
      message_id: request.message_id,
      session_id: request.session_id,
      role: "user",
      content: request.content,
      attachments: request.attachments ?? [],
      metadata: {
        ...request.message_metadata,
        source: "pending",
        job_id: request.job_id,
        delivery_policy: request.delivery_policy,
      },
      created_at: request.created_at,
      updated_at: request.updated_at,
    },
    assistantMessages: [],
    events: [],
    status: "queued",
    jobId: request.job_id,
    pending: true,
    deliveryPolicy: request.delivery_policy,
    enqueueSequence: request.enqueue_sequence,
    waitingReason: request.waiting_reason,
    queueSnapshotVersion: snapshot.snapshot_version,
    pendingPosition: request.position,
    source: "pending",
    }));
}

export function removePendingForTraceEvent(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  event: TraceEvent,
  mapKey: string = sessionId,
) {
  if (!isJobTerminalTraceType(event.type)) {
    return;
  }
  const pendingList = map.get(mapKey) ?? [];
  if (pendingList.length === 0) {
    return;
  }

  writePendingList(
    map,
    sessionId,
    pendingList.filter(
      (conversation) => !conversationMatchesTraceEvent(conversation, event),
    ),
    mapKey,
  );
}

export function preservePendingTerminalConversation(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  event: TraceEvent,
  turnStatus: Extract<JobStatus, "completed" | "failed" | "cancelled" | "timed_out">,
  mapKey: string = sessionId,
): void {
  const pendingList = map.get(mapKey) ?? [];
  const pendingIndex = pendingList.findIndex((conversation) =>
    conversationMatchesTraceEvent(conversation, event),
  );
  if (pendingIndex === -1) return;

  const pending = pendingList[pendingIndex];
  const events = compactPendingConversationEvents(
    dedupeTraceEvents([...pending.events, event]),
  );
  const updatedPending: ConversationView = {
    ...pending,
    events,
    status: turnStatus === "completed" ? "done" : "error",
    turnStatus,
    pending: false,
    activeJobOverlay: false,
  };
  const next = [...pendingList];
  next[pendingIndex] = updatedPending;
  writePendingList(map, sessionId, next, mapKey);
}

export function completePendingForJob(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  jobId: string,
  turnStatus: Extract<JobStatus, "completed" | "failed" | "cancelled" | "timed_out">,
  mapKey: string = sessionId,
): void {
  if (!jobId) return;
  const pendingList = map.get(mapKey) ?? [];
  const pendingIndex = pendingList.findIndex(
    (conversation) => conversation.jobId === jobId,
  );
  if (pendingIndex === -1) return;

  const next = [...pendingList];
  next[pendingIndex] = {
    ...next[pendingIndex],
    status: turnStatus === "completed" ? "done" : "error",
    turnStatus,
    pending: false,
    activeJobOverlay: false,
  };
  writePendingList(map, sessionId, next, mapKey);
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

export function getConversationsForSession(
  sessionId: string,
  state: AppState,
  sessionCacheKey: string = sessionId,
): ConversationView[] {
  const turnConversations = turnTimelineConversations(
    state,
    sessionCacheKey,
    sessionId,
  );
  const pendingList = state.pendingConversations.get(sessionCacheKey) ?? [];

  if (pendingList.length === 0) {
    return applyMessageStreamProjection(
      turnConversations,
      state.messageStreamsByTurnStream ?? new Map(),
    );
  }

  const merged = [...turnConversations];
  for (const pending of pendingList) {
    const matchedIndex = merged.findIndex((conversation) =>
      conversationsMatch(conversation, pending),
    );
    if (matchedIndex === -1) {
      merged.push({ ...pending, source: "pending" });
      continue;
    }

    merged[matchedIndex] = mergeConversation(merged[matchedIndex], pending);
  }

  return applyMessageStreamProjection(
    sortConversationViews(dedupeConversationViews(merged)),
    state.messageStreamsByTurnStream ?? new Map(),
  );
}

function applyMessageStreamProjection(
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
