import type { Message, PendingRequestList, TraceEvent } from "../types/backend";
import type { AppState, ConversationView } from "../types/frontend";
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

  return {
    conversationId: turn.turn_id,
    turnId: turn.turn_id,
    turnRevision: turn.revision,
    turnItemsView: isTurnDetail(turn) ? "full" : "summary",
    sessionId: turn.session_id,
    userMessage,
    assistantMessages,
    events: isTurnDetail(turn) ? (turn.items ?? []) as TraceEvent[] : [],
    status: turnConversationStatus(turn),
    jobId: turn.job_id,
    pending: false,
    source: "turn",
  };
}

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
    return turn?.session_id === sessionId ? [conversationFromTurn(turn)] : [];
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
        (left.pendingPosition ?? Number.MAX_SAFE_INTEGER)
        - (right.pendingPosition ?? Number.MAX_SAFE_INTEGER)
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
    userMessage: persisted.userMessage ?? pending.userMessage,
    assistantMessages,
    events: dedupeTraceEvents([...persisted.events, ...pending.events]),
    source: persisted.source,
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
  for (const event of dedupeTraceEvents(events)) {
    if (event.type === "status_change") {
      status =
        tracePayloadString(event, "status") === "queued"
          ? "queued"
          : "running";
      continue;
    }

    if (event.type === "job_completed") {
      status = "done";
      continue;
    }
    if (event.type === "job_failed" || event.type === "job_cancelled") {
      status = "error";
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
      status = terminalStatusForEvent(event.type);
    }
  }
  return status;
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
  const existingActiveConversation = snapshot.active_job_id
    ? (pendingMap.get(mapKey) ?? []).find(
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
      ?? createActiveJobOverlay(snapshot.session_id, snapshot.active_job_id),
    );
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
  );
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
  return (snapshot.requests ?? []).map((request) => ({
    conversationId: request.message_id,
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
        pending_kind: request.kind,
      },
      created_at: request.created_at,
      updated_at: request.updated_at,
    },
    assistantMessages: [],
    events: [],
    status: "queued",
    jobId: request.job_id,
    pending: true,
    pendingKind: request.kind,
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
    return dedupeConversationViews(turnConversations);
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

  return sortConversationViews(dedupeConversationViews(merged));
}
