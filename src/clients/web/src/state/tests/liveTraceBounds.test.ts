import { describe, expect, test } from "bun:test";
import {
  appendTraceEventsToPendingConversations,
  PENDING_CONVERSATION_EVENT_LIMIT,
} from "../conversations";
import { aggregateConversationEvents } from "../trace/traceAggregation";
import {
  appendBoundedLiveTraceEvents,
  appendReceivedEvents,
  FRONTEND_EVENT_QUEUE_LIMIT,
  LIVE_TRACE_EVENT_LIMIT,
} from "../traceEvents";
import type { TraceEvent } from "../../types/backend";
import type { ConversationView, FrontendReceivedEvent } from "../../types/frontend";

const SESSION_ID = "session-live-bound";
const JOB_ID = "job-live-bound";

function trace(
  index: number,
  type: TraceEvent["type"] = "text_delta",
): TraceEvent {
  const text = type === "text_delta" ? "x" : "";
  return {
    event_id: `event-${index}`,
    part_id: "part-live-bound",
    session_id: SESSION_ID,
    job_id: JOB_ID,
    step_id: null,
    agent_id: "default",
    timestamp: new Date(index).toISOString(),
    type,
    phase: "text",
    title: type,
    content: text,
    payload: { kind: "markdown", text },
    raw: { payload: { kind: "markdown", text } },
  };
}

describe("Live Trace 有界保留", () => {
  test("全局镜像和事件视图队列均保持固定上限且保留终态", () => {
    const incoming = Array.from(
      { length: LIVE_TRACE_EVENT_LIMIT * 3 },
      (_, index) => trace(index),
    );
    incoming.push(trace(incoming.length, "job_completed"));
    const live = appendBoundedLiveTraceEvents([], incoming);
    const eventQueues = new Map<string, FrontendReceivedEvent[]>();
    appendReceivedEvents(eventQueues, SESSION_ID, incoming, "sse");

    expect(live).toHaveLength(LIVE_TRACE_EVENT_LIMIT);
    expect(live[live.length - 1]?.type).toBe("job_completed");
    expect(eventQueues.get(SESSION_ID)?.length).toBeLessThanOrEqual(
      FRONTEND_EVENT_QUEUE_LIMIT,
    );
    expect(
      eventQueues.get(SESSION_ID)?.some(
        (event) => event.kind === "trace" && event.event.type === "job_completed",
      ),
    ).toBe(true);
  });

  test("pending Job 压实 delta 对象但保持完整流式正文", () => {
    const conversation: ConversationView = {
      conversationId: "message-live-bound",
      displayMode: "live",
      sessionId: SESSION_ID,
      userMessage: null,
      assistantMessages: [],
      events: [],
      status: "running",
      jobId: JOB_ID,
      pending: true,
      source: "pending",
    };
    const pending = new Map([[SESSION_ID, [conversation]]]);
    const deltaCount = PENDING_CONVERSATION_EVENT_LIMIT * 3;
    appendTraceEventsToPendingConversations(
      pending,
      SESSION_ID,
      Array.from({ length: deltaCount }, (_, index) => trace(index)),
    );

    const events = pending.get(SESSION_ID)?.[0]?.events ?? [];
    const response = aggregateConversationEvents(events, JOB_ID, true).find(
      (item) => item.kind === "aggregated_text" && item.partKind === "markdown",
    );
    expect(events.length).toBeLessThanOrEqual(PENDING_CONVERSATION_EVENT_LIMIT);
    expect(response && "text" in response ? response.text.length : 0).toBe(deltaCount);
  });
});
