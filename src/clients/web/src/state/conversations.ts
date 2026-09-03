import type {
  JobStatus,
  Message,
  PendingRequestList,
  TraceEvent,
} from "../types/backend";
import type { AppState, ConversationView } from "../types/frontend";
import {
  messageStreamToResponseParts,
  type MessageStreamState,
} from "./messageStream";
import { isTurnDetail, type TurnRecord } from "./session/turnTimeline";
import {
  dedupeTraceEvents,
  isJobTerminalTraceType,
  isTerminalTraceType,
  terminalStatusForEvent,
  traceJobId,
  tracePayloadString,
} from "./traceEvents";

export const PENDING_CONVERSATION_EVENT_LIMIT = 512;

function compactPendingConversationEvents(
  events: TraceEvent[],
  limit: number = PENDING_CONVERSATION_EVENT_LIMIT,
): TraceEvent[] {
  if (events.length <= limit) return events;
  const provisionalTail = events.slice(-Math.floor(limit / 2));
  const activePartIds = new Set(
    provisionalTail.flatMap((event) =>
      event.part_id && ["text_start", "text_delta", "text_end"].includes(event.type)
        ? [event.part_id]
        : [],
    ),
  );
  const summarizedPartIds = [...new Set(events.flatMap((event) =>
    event.type === "text_delta" && event.part_id && activePartIds.has(event.part_id)
      ? [event.part_id]
      : [],
  ))].slice(-Math.floor(limit / 2));
  if (summarizedPartIds.length === 0) return events.slice(-limit);

  const tailBudget = limit - summarizedPartIds.length;
  const prefix = events.slice(0, events.length - tailBudget);
  const selectedParts = new Set(summarizedPartIds);
  const summaries = new Map<string, { first: TraceEvent; last: TraceEvent; text: string }>();
  for (const event of prefix) {
    if (event.type !== "text_delta" || !event.part_id || !selectedParts.has(event.part_id)) {
      continue;
    }
    const current = summaries.get(event.part_id);
    const text = tracePayloadString(event, "text");
    summaries.set(event.part_id, current
      ? { ...current, last: event, text: current.text + text }
      : { first: event, last: event, text });
  }
  const compacted = [...summaries.entries()].map(([partId, summary]) => ({
    ...summary.last,
    event_id: `compacted:${partId}:${summary.last.event_id}`,
    part_id: partId,
    content: summary.text,
    payload: { ...(summary.last.payload ?? {}), text: summary.text },
    raw: summary.last.raw
      ? {
          ...summary.last.raw,
          payload: { ...(summary.last.raw.payload ?? {}), text: summary.text },
        }
      : summary.last.raw,
  }));
  return [...compacted, ...events.slice(-tailBudget)];
}

function turnConversationStatus(
  turn: TurnRecord,
): ConversationView["status"] {
  if (turn.status === "accepted" || turn.status === "queued") {
    return "queued";
  }
  if (turn.status === "completed" || turn.status === "succeeded") {
    return "done";
  }
  if (
    turn.status === "failed"
    || turn.status === "cancelled"
    || turn.status === "timed_out"
  ) {
    return "error";
  }
  return "running";
}

function conversationFromTurn(turn: TurnRecord): ConversationView {
  const userMessages = turn.user_messages ?? [];
  const firstUserMessage = userMessages[0];
  const userContent = userMessages.map((message) =>
    "content" in message ? message.content : message.preview ?? "",
  ).join("\n\n");
  const attachments = isTurnDetail(turn)
    ? userMessages.flatMap((message) =>
      "attachments" in message ? message.attachments ?? [] : [],
    )
    : [];
  const userMessage: Message | null = firstUserMessage
    ? {
        message_id: firstUserMessage.message_id,
        session_id: turn.session_id,
        role: "user",
        content: userContent,
        attachments,
        metadata: {
          ...("metadata" in firstUserMessage
            ? firstUserMessage.metadata ?? {}
            : {}),
          source: "turn_projection",
          job_id: turn.job_id,
          turn_id: turn.turn_id,
          turn_revision: turn.revision,
          summary: !isTurnDetail(turn),
        },
        created_at: firstUserMessage.created_at,
        updated_at: turn.updated_at,
      }
    : null;
  const assistantContent = isTurnDetail(turn)
    ? turn.final_response ?? turn.response_preview ?? ""
    : turn.response_preview ?? "";
  const assistantMessages: Message[] = assistantContent
    ? [{
        message_id: `${turn.turn_id}:assistant`,
        session_id: turn.session_id,
        role: "assistant",
        content: assistantContent,
        attachments: [],
        metadata: {
          source: "turn_projection",
          job_id: turn.job_id,
          turn_id: turn.turn_id,
          turn_revision: turn.revision,
          summary: !isTurnDetail(turn),
        },
        created_at: turn.completed_at ?? turn.updated_at,
        updated_at: turn.updated_at,
      }]
    : [];
  const thinkingBlocks = (turn.thinking_blocks ?? []).map((block) => ({
    kind: block.kind,
    text: block.text ?? "",
  }));
  const toolSummary = turn.tool_summary ?? [];

  return {
    conversationId: turn.turn_id,
    displayMode: "history",
    turnId: turn.turn_id,
    turnRevision: turn.revision,
    turnItemsView: isTurnDetail(turn) ? "full" : "summary",
    turnStatus: turn.status as ConversationView["turnStatus"],
    activityStats: turn.activity_stats
      ? {
          duration_ms: turn.activity_stats.duration_ms ?? null,
          message_count: turn.activity_stats.message_count ?? 0,
        }
      : undefined,
    sessionId: turn.session_id,
    userMessage,
    assistantMessages,
    thinkingBlocks,
    toolSummary,
    responseParts: turn.response_parts ?? [],
    events: isTurnDetail(turn) ? (turn.items ?? []) as TraceEvent[] : [],
    status: turnConversationStatus(turn),
    jobId: turn.job_id,
    pending: false,
    source: "turn",
  };
}

const turnConversationCache = new WeakMap<TurnRecord, ConversationView>();

function turnTimelineConversations(
  state: AppState,
  sessionCacheKey: string,
  sessionId: string,
): ConversationView[] {
  const timeline = state.turnTimelinesBySession?.get(sessionCacheKey);
  if (!timeline) {
    return [];
  }
  return timeline.orderedTurnIds.flatMap((turnId) => {
    const turn = timeline.turnsById[turnId];
    if (turn?.session_id !== sessionId) {
      return [];
    }
    const cached = turnConversationCache.get(turn);
    if (cached) {
      return [cached];
    }
    const conversation = conversationFromTurn(turn);
    turnConversationCache.set(turn, conversation);
    return [conversation];
  });
}

function conversationStartTime(conversation: ConversationView): number {
  const messageTime = conversation.userMessage?.created_at;
  if (messageTime) {
    return new Date(messageTime).getTime();
  }
  const firstEvent = conversation.events[0];
  return firstEvent ? new Date(firstEvent.timestamp).getTime() : 0;
}

export function sortConversationViews(
  conversations: ConversationView[],
): ConversationView[] {
  return [...conversations].sort((left, right) => {
    if (left.pending !== right.pending) {
      return left.pending ? 1 : -1;
    }
    if (left.pending && right.pending) {
      return (
        (left.enqueueSequence ?? left.pendingPosition ?? Number.MAX_SAFE_INTEGER)
        - (right.enqueueSequence ?? right.pendingPosition ?? Number.MAX_SAFE_INTEGER)
      );
    }
    return conversationStartTime(left) - conversationStartTime(right);
  });
}

function conversationIdentityKey(conversation: ConversationView): string | null {
  const messageId = conversation.userMessage?.message_id ?? "";
  if (messageId) {
    return `message:${messageId}`;
  }

  const jobId = conversation.jobId ?? "";
  if (jobId) {
    return `job:${jobId}`;
  }

  return null;
}

function conversationsMatch(
  left: ConversationView,
  right: ConversationView,
): boolean {
  const leftMessageId = left.userMessage?.message_id ?? "";
  const rightMessageId = right.userMessage?.message_id ?? "";
  if (leftMessageId && rightMessageId && leftMessageId === rightMessageId) {
    return true;
  }

  const leftJobId = left.jobId ?? "";
  const rightJobId = right.jobId ?? "";
  return Boolean(leftJobId && rightJobId && leftJobId === rightJobId);
}

function mergeConversation(
  persisted: ConversationView,
  pending: ConversationView,
): ConversationView {
  const assistantMessages = [
    ...(persisted.assistantMessages ?? []),
    ...(pending.assistantMessages ?? []),
  ].filter(
    (message, index, all) =>
      all.findIndex((candidate) => candidate.message_id === message.message_id) === index,
  );
  return {
    ...persisted,
    ...pending,
    displayMode: pending.displayMode,
    userMessage: persisted.userMessage && pending.userMessage
      ? {
          ...persisted.userMessage,
          ...pending.userMessage,
          // Turn 详情/摘要可能先于 live 状态到达；保留乐观 replay
          // 的操作元数据，否则回退提示会在新 Job 运行期间消失。
          metadata: {
            ...persisted.userMessage.metadata,
            ...pending.userMessage.metadata,
          },
        }
      : persisted.userMessage ?? pending.userMessage,
    assistantMessages,
    events: dedupeTraceEvents([...persisted.events, ...pending.events]),
    source: pending.source === "pending" ? "pending" : persisted.source,
  };
}

function dedupeConversationViews(
  conversations: ConversationView[],
): ConversationView[] {
  const merged: ConversationView[] = [];
  const seen = new Map<string, number>();

  for (const conversation of conversations) {
    const identityKey = conversationIdentityKey(conversation);
    if (!identityKey) {
      merged.push(conversation);
      continue;
    }

    const existingIndex = seen.get(identityKey);
    if (existingIndex === undefined) {
      seen.set(identityKey, merged.length);
      merged.push(conversation);
      continue;
    }

    merged[existingIndex] = mergeConversation(
      merged[existingIndex],
      conversation,
    );
  }

  return merged;
}

export function conversationMatchesTraceEvent(
  conversation: ConversationView,
  event: TraceEvent,
): boolean {
  const eventJobId = traceJobId(event);
  if (eventJobId && conversation.jobId === eventJobId) {
    return true;
  }

  const eventMessageId = tracePayloadString(event, "message_id");
  const conversationMessageId = conversation.userMessage?.message_id ?? "";
  return Boolean(eventMessageId && conversationMessageId === eventMessageId);
}

export function appendTraceEventsToPendingConversations(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  traceEvents: TraceEvent[],
  mapKey: string = sessionId,
  fallbackToSinglePending: boolean = false,
): void {
  let pendingList = map.get(mapKey) ?? [];
  if (pendingList.length === 0 || traceEvents.length === 0) {
    return;
  }

  const eventsByPendingIndex = new Map<number, TraceEvent[]>();
  for (const traceEvent of traceEvents) {
    let pendingIndex = pendingList.findIndex((conversation) =>
      conversationMatchesTraceEvent(conversation, traceEvent),
    );
    if (
      pendingIndex === -1
      && fallbackToSinglePending
      && pendingList.length === 1
    ) {
      pendingIndex = 0;
    }
    if (pendingIndex === -1) {
      continue;
    }
    const matchedEvents = eventsByPendingIndex.get(pendingIndex) ?? [];
    matchedEvents.push(traceEvent);
    eventsByPendingIndex.set(pendingIndex, matchedEvents);
  }

  for (const [pendingIndex, matchedEvents] of eventsByPendingIndex) {
    const pending = pendingList[pendingIndex];
    const events = compactPendingConversationEvents(
      dedupeTraceEvents([...pending.events, ...matchedEvents]),
    );
    const terminal = events.some((event) => isTerminalTraceType(event.type));
    const updatedPending: ConversationView = {
      ...pending,
      events,
      status: statusForConversationEvents(events, pending.status),
      pending: terminal ? false : pending.pending,
    };
    const updatedPendingList = [...pendingList];
    updatedPendingList[pendingIndex] = updatedPending;
    pendingList = updatedPendingList;
  }

  writePendingList(map, sessionId, pendingList, mapKey);
}

export function traceEventsForConversation(
  traceEvents: TraceEvent[],
  conversation: ConversationView,
): TraceEvent[] {
  return traceEvents.filter((event) =>
    conversationMatchesTraceEvent(conversation, event),
  );
}

export function statusForConversationEvents(
  events: TraceEvent[],
  fallback: ConversationView["status"],
): ConversationView["status"] {
  let status = fallback;
  let terminalStatus: ConversationView["status"] | null = null;
  for (const event of dedupeTraceEvents(events)) {
    if (terminalStatus !== null) {
      // 终态一旦落入本地镜像，迟到的旧 SSE 不能把它重新改成 running。
      continue;
    }
    if (event.type === "status_change") {
      status =
        tracePayloadString(event, "status") === "queued"
          ? "queued"
          : "running";
      continue;
    }

    if (event.type === "job_completed") {
      terminalStatus = "done";
      continue;
    }
    if (event.type === "job_failed" || event.type === "job_cancelled") {
      terminalStatus = "error";
      continue;
    }
    if (
      [
        "job_created",
        "message_created",
        "job_started",
        "agent_start",
        "llm_request",
        "text_start",
        "text_delta",
        "tool_call_start",
      ].includes(event.type)
    ) {
      status = "running";
      continue;
    }

    if (isTerminalTraceType(event.type)) {
      terminalStatus = terminalStatusForEvent(event.type);
    }
  }
  return terminalStatus ?? status;
}

export function hasJobTerminalTraceEvent(events: TraceEvent[]): boolean {
  return events.some((event) => isJobTerminalTraceType(event.type));
}

export function writePendingList(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  list: ConversationView[],
  mapKey: string = sessionId,
) {
  if (list.length === 0) {
    map.delete(mapKey);
    return;
  }
  map.set(mapKey, list);
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
      Number(isTerminalMessageStreamStatus(right.streamStatus))
        - Number(isTerminalMessageStreamStatus(left.streamStatus))
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
        ...(isTerminalMessageStreamStatus(stream.streamStatus)
          ? { responseParts: messageStreamToResponseParts(stream) }
          : {}),
        status: terminalConversationStatus,
        activeJobOverlay: false,
        pending: false,
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
    }
    const responseParts = messageStreamToResponseParts(stream);
    const terminalStatus = stream.streamStatus === "completed"
      ? "done"
      : stream.streamStatus === "interrupted" || stream.streamStatus === "failed"
        ? "error"
        : "running";
    return {
      ...conversation,
      responseParts,
      status: terminalStatus,
      activeJobOverlay: !isTerminalMessageStreamStatus(stream.streamStatus),
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

const TERMINAL_TURN_STATUSES = new Set([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);

function isTerminalMessageStreamStatus(
  status: MessageStreamState["streamStatus"],
): boolean {
  return status === "completed" || status === "interrupted" || status === "failed";
}
