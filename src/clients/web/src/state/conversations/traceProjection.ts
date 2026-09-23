import type { TraceEvent } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import {
  dedupeTraceEvents,
  isJobTerminalTraceType,
  isTerminalTraceType,
  terminalStatusForEvent,
  traceJobId,
  tracePayloadString,
} from "../traceEvents";
import { writePendingList } from "./pendingMap";

export const PENDING_CONVERSATION_EVENT_LIMIT = 512;

/** 把同一个 part 的 text_delta 折叠成一条汇总事件，保证 pending 事件列表有界。 */
export function compactPendingConversationEvents(
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
