import type {
  AttachmentRef,
  Message,
  PendingRequestList,
  TraceEvent,
} from "../types/backend";
import type { AppState, ConversationView } from "../types/frontend";
import {
  dedupeTraceEvents,
  isJobTerminalTraceType,
  isTerminalTraceType,
  rawTracePayload,
  terminalStatusForEvent,
  traceJobId,
  tracePayloadString,
} from "./traceEvents";

function groupMessagesIntoConversations(
  messages: Message[],
): ConversationView[] {
  const conversations: ConversationView[] = [];
  let current: ConversationView | null = null;
  const seenUserMessageIds = new Set<string>();

  for (const message of messages) {
    if (message.role === "user") {
      const messageId = message.message_id;
      if (seenUserMessageIds.has(messageId)) {
        continue;
      }
      seenUserMessageIds.add(messageId);
      current = {
        conversationId: messageId,
        sessionId: message.session_id,
        userMessage: message,
        assistantMessages: [],
        events: [],
        status: "done",
        jobId: String(message.metadata?.job_id ?? "") || null,
        pending: false,
        source: "messages",
      };
      conversations.push(current);
      continue;
    }

    if (!current) {
      current = {
        conversationId: message.message_id,
        sessionId: message.session_id,
        userMessage: null,
        assistantMessages: [],
        events: [],
        status: "done",
        jobId: String(message.metadata?.job_id ?? "") || null,
        pending: false,
        source: "messages",
      };
      conversations.push(current);
    }
    if (message.role === "assistant") {
      current.assistantMessages = [...(current.assistantMessages ?? []), message];
    }
  }

  return conversations;
}

function attachTraceEventsToConversations(
  conversations: ConversationView[],
  traceEvents: TraceEvent[],
): ConversationView[] {
  if (conversations.length === 0 || traceEvents.length === 0) {
    return conversations;
  }

  const dedupedEvents = dedupeTraceEvents(traceEvents);
  interface MessageBoundary {
    messageId: string;
    jobId: string;
    timestamp: number;
  }
  const boundaries: MessageBoundary[] = [];
  for (const event of dedupedEvents) {
    if (event.type === "message_created") {
      const innerPayload = event.raw?.payload ?? event.payload ?? {};
      const msgId =
        typeof innerPayload.message_id === "string"
          ? innerPayload.message_id
          : "";
      if (msgId) {
        boundaries.push({
          messageId: msgId,
          jobId: traceJobId(event),
          timestamp: new Date(event.timestamp).getTime(),
        });
      }
    }
  }

  const boundaryTs = boundaries.map((b) => b.timestamp);

  return conversations.map((conversation) => {
    const userMsgId = conversation.userMessage?.message_id ?? "";
    const boundaryIndex = boundaries.findIndex(
      (b) => b.messageId === userMsgId,
    );
    if (boundaryIndex === -1) {
      return conversation;
    }

    const jobId = conversation.jobId ?? boundaries[boundaryIndex]?.jobId ?? "";
    if (jobId) {
      const convEvents = dedupedEvents.filter(
        (event) => traceJobId(event) === jobId,
      );
      return {
        ...conversation,
        jobId,
        events: convEvents,
        status: statusForConversationEvents(convEvents, conversation.status),
      };
    }

    const startTs = boundaryTs[boundaryIndex];
    const endTs =
      boundaryIndex + 1 < boundaryTs.length
        ? boundaryTs[boundaryIndex + 1]
        : Infinity;

    const convEvents = dedupedEvents.filter((event) => {
      const eventTs = new Date(event.timestamp).getTime();
      return eventTs >= startTs && eventTs < endTs;
    });

    return {
      ...conversation,
      events: convEvents,
      status: statusForConversationEvents(convEvents, conversation.status),
    };
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
  writePendingList(
    pendingMap,
    snapshot.session_id,
    pendingSnapshotToConversations(snapshot),
    mapKey,
  );
  if (snapshot.active_job_id) {
    activeJobMap.set(mapKey, snapshot.active_job_id);
  } else {
    activeJobMap.delete(mapKey);
  }
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

function buildTraceOnlyConversations(
  sessionId: string,
  traceEvents: TraceEvent[],
): ConversationView[] {
  const conversations: ConversationView[] = [];
  const seenMessageIds = new Set<string>();

  for (const event of dedupeTraceEvents(traceEvents)) {
    if (event.type !== "message_created") {
      continue;
    }

    const payload = rawTracePayload(event);
    const payloadSessionId =
      typeof payload.session_id === "string" ? payload.session_id : sessionId;
    const role = payload.role === "user" ? "user" : null;
    if (payloadSessionId !== sessionId || role !== "user") {
      continue;
    }

    const messageId =
      typeof payload.message_id === "string"
        ? payload.message_id
        : event.event_id;
    if (seenMessageIds.has(messageId)) {
      continue;
    }
    seenMessageIds.add(messageId);
    const content = typeof payload.content === "string" ? payload.content : "";
    const timestamp =
      typeof payload.created_at === "string"
        ? payload.created_at
        : event.timestamp;
    const hasFailure = traceEvents.some(
      (trace) =>
        trace.job_id === event.job_id &&
        ["job_failed", "job_cancelled", "session_interrupted"].includes(
          trace.type,
        ),
    );
    const hasCompletion = traceEvents.some(
      (trace) =>
        trace.job_id === event.job_id &&
        ["agent_end", "job_completed"].includes(trace.type),
    );

    const attachments = Array.isArray(payload.attachments)
      ? (payload.attachments as AttachmentRef[])
      : [];

    conversations.push({
      conversationId: messageId,
      sessionId,
      userMessage: {
        message_id: messageId,
        session_id: sessionId,
        role,
        content,
        attachments,
        metadata: { source: "trace", job_id: event.job_id },
        created_at: timestamp,
        updated_at: timestamp,
      },
      assistantMessages: [],
      events: [],
      status: hasFailure ? "error" : hasCompletion ? "done" : "running",
      jobId: event.job_id,
      pending: false,
      source: "messages",
    });
  }

  return conversations;
}

export function getConversationsForSession(
  sessionId: string,
  state: AppState,
  sessionCacheKey: string = sessionId,
): ConversationView[] {
  const messageConversations = groupMessagesIntoConversations(
    state.messages.filter((message) => message.session_id === sessionId),
  );
  const conversations =
    messageConversations.length > 0
      ? messageConversations
      : buildTraceOnlyConversations(sessionId, state.traceEvents);
  const withTraceEvents = attachTraceEventsToConversations(
    conversations,
    state.traceEvents,
  );
  const pendingList = state.pendingConversations.get(sessionCacheKey) ?? [];

  if (pendingList.length === 0) {
    return dedupeConversationViews(withTraceEvents);
  }

  const merged = [...withTraceEvents];
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
