import type { TraceEvent } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import {
  dedupeTraceEvents,
  isJobTerminalTraceType,
  isTerminalTraceType,
  terminalStatusForEvent,
  traceJobId,
  tracePayloadString,
  rawTracePayload,
} from "../traceEvents";
import { writePendingList } from "./pendingMap";

export const PENDING_CONVERSATION_EVENT_LIMIT = 512;

/** 正文由 message stream 提供；这里只保留最近的真实 Trace，不能另行拼接全量正文。 */
export function compactPendingConversationEvents(
  events: TraceEvent[],
  limit: number = PENDING_CONVERSATION_EVENT_LIMIT,
): TraceEvent[] {
  return events.length <= limit ? events : events.slice(-limit);
}

export function conversationMatchesTraceEvent(
  conversation: ConversationView,
  event: TraceEvent,
): boolean {
  if (!conversationOwnsTraceEvent(conversation, event)) return false;
  const eventJobId = traceJobId(event);
  if (eventJobId && conversation.jobId === eventJobId) {
    return true;
  }

  const eventMessageId = tracePayloadString(event, "message_id");
  const conversationMessageId = conversation.userMessage?.message_id ?? "";
  return Boolean(eventMessageId && conversationMessageId === eventMessageId);
}

function conversationOwnsTraceEvent(conversation: ConversationView, event: TraceEvent): boolean {
  return conversation.sessionId === event.session_id
    && (conversation.threadId ?? conversation.userMessage?.thread_id) === event.thread_id;
}

function traceMatchKeys(
  sessionId: string | undefined,
  threadId: string | null | undefined,
  jobId: string | undefined,
  messageId: string | undefined,
): string[] {
  const owner = JSON.stringify([
    [typeof sessionId, sessionId],
    [typeof threadId, threadId],
  ]);
  const keys: string[] = [];
  if (jobId) keys.push(`${owner}:job:${JSON.stringify(jobId)}`);
  if (messageId) keys.push(`${owner}:message:${JSON.stringify(messageId)}`);
  return keys;
}

function traceMatchKeysForConversation(conversation: ConversationView): string[] {
  return traceMatchKeys(
    conversation.sessionId,
    conversation.threadId ?? conversation.userMessage?.thread_id,
    conversation.jobId ?? undefined,
    conversation.userMessage?.message_id,
  );
}

function traceMatchKeysForEvent(event: TraceEvent): string[] {
  return traceMatchKeys(
    event.session_id,
    event.thread_id,
    traceJobId(event),
    tracePayloadString(event, "message_id"),
  );
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

  const pendingIndexByTraceKey = new Map<string, number>();
  for (let index = 0; index < pendingList.length; index += 1) {
    for (const key of traceMatchKeysForConversation(pendingList[index]!)) {
      if (!pendingIndexByTraceKey.has(key)) {
        pendingIndexByTraceKey.set(key, index);
      }
    }
  }

  const eventsByPendingIndex = new Map<number, TraceEvent[]>();
  for (const traceEvent of traceEvents) {
    let pendingIndex = -1;
    for (const key of traceMatchKeysForEvent(traceEvent)) {
      const candidateIndex = pendingIndexByTraceKey.get(key);
      if (candidateIndex !== undefined
        && (pendingIndex === -1 || candidateIndex < pendingIndex)
        && conversationMatchesTraceEvent(pendingList[candidateIndex]!, traceEvent)) {
        pendingIndex = candidateIndex;
      }
    }
    if (
      pendingIndex === -1
      && fallbackToSinglePending
      && pendingList.length === 1
      && conversationOwnsTraceEvent(pendingList[0], traceEvent)
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
      // 队列通知描述整个 Thread，可能同时指向活动 Job 和队首消息；
      // 只触发权威快照读取，不能据此把队首标成正在执行。
      if (typeof rawTracePayload(event).queue_snapshot_version === "number") continue;
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
