import type { AttachmentRef, Message, TraceEvent } from "../types/backend";
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

  for (const message of messages) {
    if (message.role === "user") {
      current = {
        conversationId:
          message.message_id || `conversation_${conversations.length}`,
        sessionId: message.session_id,
        userMessage: message,
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
        conversationId:
          message.message_id || `conversation_${conversations.length}`,
        sessionId: message.session_id,
        userMessage: null,
        events: [],
        status: "done",
        jobId: String(message.metadata?.job_id ?? "") || null,
        pending: false,
        source: "messages",
      };
      conversations.push(current);
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
  return {
    ...persisted,
    ...pending,
    userMessage: persisted.userMessage ?? pending.userMessage,
    events: dedupeTraceEvents([...persisted.events, ...pending.events]),
    source: persisted.source,
  };
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

    if (
      [
        "job_created",
        "message_created",
        "job_started",
        "agent_start",
        "llm_request",
        "text_start",
        "text_delta",
        "text_end",
        "tool_call_start",
        "tool_call_end",
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
) {
  if (list.length === 0) {
    map.delete(sessionId);
    return;
  }
  map.set(sessionId, list);
}

export function removePendingForTraceEvent(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  event: TraceEvent,
) {
  const pendingList = map.get(sessionId) ?? [];
  if (pendingList.length === 0) {
    return;
  }

  writePendingList(
    map,
    sessionId,
    pendingList.filter(
      (conversation) => !conversationMatchesTraceEvent(conversation, event),
    ),
  );
}

function buildTraceOnlyConversations(
  sessionId: string,
  traceEvents: TraceEvent[],
): ConversationView[] {
  const conversations: ConversationView[] = [];

  for (const event of traceEvents) {
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
      events: [],
      status: hasFailure ? "error" : hasCompletion ? "done" : "running",
      jobId: event.job_id ?? null,
      pending: false,
      source: "messages",
    });
  }

  return conversations;
}

export function getConversationsForSession(
  sessionId: string,
  state: AppState,
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
  const pendingList = state.pendingConversations.get(sessionId) ?? [];

  if (pendingList.length === 0) {
    return withTraceEvents;
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

  return merged.sort(
    (a, b) => conversationStartTime(a) - conversationStartTime(b),
  );
}
