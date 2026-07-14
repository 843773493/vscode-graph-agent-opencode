import type { SessionStreamEvent } from "../api";
import type { TraceEvent } from "../types/backend";
import type {
  ConversationView,
  FrontendEventSource,
  FrontendReceivedEvent,
} from "../types/frontend";

export const FRONTEND_EVENT_QUEUE_LIMIT = 200;

export function rawTracePayload(event: TraceEvent): Record<string, unknown> {
  return event.raw?.payload ?? event.payload ?? {};
}

export function traceJobId(event: TraceEvent): string {
  return event.job_id;
}

export function tracePayloadString(event: TraceEvent, key: string): string {
  const payload = rawTracePayload(event);
  const value = payload[key];
  return typeof value === "string" ? value : "";
}

export function dedupeTraceEvents(events: TraceEvent[]): TraceEvent[] {
  const seenEventIds = new Set<string>();
  return events
    .filter((event) => {
      const id = event.event_id;
      if (!id) {
        throw new Error(`Trace 事件缺少 event_id: type=${event.type}`);
      }
      if (seenEventIds.has(id)) {
        return false;
      }
      seenEventIds.add(id);
      return true;
    })
    .sort(
      (a, b) =>
        new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(),
    );
}

function frontendEventQueueId(event: TraceEvent, source: FrontendEventSource) {
  return `${source}:${event.event_id}`;
}

function traceEventSessionId(event: TraceEvent): string {
  return event.session_id;
}

function traceEventWithSession(
  event: TraceEvent,
  sessionId: string,
): TraceEvent {
  if (event.session_id === sessionId) {
    return event;
  }
  throw new Error(
    `Trace 事件 session_id 不一致: expected=${sessionId} actual=${event.session_id} event_id=${event.event_id}`,
  );
}

export function appendReceivedEvents(
  map: Map<string, FrontendReceivedEvent[]>,
  sessionId: string,
  events: TraceEvent[],
  source: FrontendEventSource,
  mapKey: string = sessionId,
) {
  if (events.length === 0) {
    return;
  }

  const current = map.get(mapKey) ?? [];
  const seen = new Set(current.map((item) => item.id));
  const receivedAt = new Date().toISOString();
  const appended: FrontendReceivedEvent[] = [];

  for (const event of events) {
    const eventSessionId = traceEventSessionId(event);
    if (eventSessionId && eventSessionId !== sessionId) {
      continue;
    }
    const id = frontendEventQueueId(event, source);
    if (seen.has(id)) {
      continue;
    }
    seen.add(id);
    appended.push({
      id,
      kind: "trace",
      sessionId,
      receivedAt,
      source,
      event: traceEventWithSession(event, sessionId),
    });
  }

  if (appended.length === 0) {
    return;
  }

  map.set(
    mapKey,
    trimReceivedEventQueue([...current, ...appended], FRONTEND_EVENT_QUEUE_LIMIT),
  );
}

function isPinnedEventQueueItem(item: FrontendReceivedEvent): boolean {
  if (item.kind === "frontend") {
    return [
      "session_load_completed",
      "session_load_failed",
      "session_selected",
    ].includes(item.type);
  }
  return ["message_created", "job_created", "job_started"].includes(
    item.event.type,
  );
}

export function trimReceivedEventQueue(
  items: FrontendReceivedEvent[],
  limit: number,
): FrontendReceivedEvent[] {
  if (items.length <= limit) {
    return items;
  }

  const pinned = items.filter(isPinnedEventQueueItem);
  const unpinned = items.filter((item) => !isPinnedEventQueueItem(item));
  const pinnedIds = new Set(pinned.map((item) => item.id));
  const keepUnpinnedCount = Math.max(limit - pinned.length, 0);
  const kept = [
    ...pinned.slice(-limit),
    ...unpinned.slice(-keepUnpinnedCount),
  ];
  return items.filter((item) => {
    if (pinnedIds.has(item.id)) {
      return kept.some((keptItem) => keptItem.id === item.id);
    }
    return kept.some((keptItem) => keptItem.id === item.id);
  });
}

export function appendFrontendEvent(
  map: Map<string, FrontendReceivedEvent[]>,
  sessionId: string,
  type: Extract<FrontendReceivedEvent, { kind: "frontend" }>["type"],
  title: string,
  payload: Record<string, unknown> = {},
  detail = "",
  mapKey: string = sessionId,
) {
  const current = map.get(mapKey) ?? [];
  const receivedAt = new Date().toISOString();
  const event: FrontendReceivedEvent = {
    id: `frontend:${type}:${receivedAt}`,
    kind: "frontend",
    sessionId,
    receivedAt,
    source: "frontend",
    type,
    title,
    detail,
    payload,
  };
  map.set(
    mapKey,
    trimReceivedEventQueue([...current, event], FRONTEND_EVENT_QUEUE_LIMIT),
  );
}

export function isTerminalTraceType(eventType: string): boolean {
  return [
    "agent_end",
    "job_completed",
    "job_failed",
    "job_cancelled",
    "session_interrupted",
  ].includes(eventType);
}

export function isJobTerminalTraceType(eventType: string): boolean {
  return [
    "job_completed",
    "job_failed",
    "job_cancelled",
    "session_interrupted",
  ].includes(eventType);
}

export function terminalStatusForEvent(
  eventType: string,
): ConversationView["status"] {
  return eventType === "job_failed" ||
    eventType === "job_cancelled" ||
    eventType === "session_interrupted"
    ? "error"
    : "done";
}

export function terminalStatusTextForEvent(eventType: string): string {
  if (eventType === "job_failed") {
    return "生成失败";
  }
  if (eventType === "job_cancelled" || eventType === "session_interrupted") {
    return "生成已中断";
  }
  return "回复已完成";
}

export function buildTraceEvent(event: SessionStreamEvent): TraceEvent {
  const raw = event.raw;
  const rawPayload =
    raw &&
    typeof raw.payload === "object" &&
    raw.payload !== null &&
    !Array.isArray(raw.payload)
      ? (raw.payload as Record<string, unknown>)
      : {};
  const payload =
    Object.keys(rawPayload).length > 0 ? rawPayload : event.payload || {};
  const normalizedRaw: TraceEvent["raw"] | undefined = raw
    ? {
        event_id:
          typeof raw.event_id === "string" ? raw.event_id : event.event_id,
        part_id:
          typeof raw.part_id === "string" ? raw.part_id : event.part_id,
        job_id:
          typeof raw.job_id === "string"
            ? raw.job_id
            : event.job_id,
        type: typeof raw.type === "string" ? raw.type : event.type,
        timestamp:
          typeof raw.timestamp === "string" ? raw.timestamp : event.timestamp,
        payload,
        session_id:
          typeof raw.session_id === "string" ? raw.session_id : undefined,
        agent_id:
          typeof raw.agent_id === "string" || raw.agent_id === null
            ? raw.agent_id
            : event.agent_id,
        step_id:
          typeof raw.step_id === "string" || raw.step_id === null
            ? raw.step_id
            : event.step_id,
      }
    : undefined;
  return {
    event_id: event.event_id,
    part_id: event.part_id ?? normalizedRaw?.part_id ?? null,
    session_id: event.session_id,
    job_id: event.job_id,
    step_id: event.step_id ?? null,
    agent_id: event.agent_id ?? null,
    timestamp: event.timestamp,
    type: event.type as TraceEvent["type"],
    payload,
    raw: normalizedRaw,
  };
}
