import type { TraceEvent } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { buildTraceTimelineItems } from "../chatTimeline";
import { buildKeyTraceSummary } from "../eventQueueDisplay";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function event(
  index: number,
  type: TraceEvent["type"],
  payload: Record<string, unknown>,
): TraceEvent {
  return {
    event_id: `evt_${index}`,
    session_id: "ses_event_display",
    job_id: "job_event_display",
    type,
    phase: type.startsWith("tool_") ? "tool" : "text",
    title: type,
    content: "",
    timestamp: `2026-07-06T00:00:0${index}.000Z`,
    payload,
    raw: { payload },
  };
}

const customToolEvents: TraceEvent[] = [
  event(1, "tool_call_start", {
    tool_name: "test_tool_2",
    args: {},
    invocation_tool_name: "invoke_custom_tool",
    tool_call_run_id: "run_test_tool_2",
  }),
  event(2, "tool_call_end", {
    tool_name: "test_tool_2",
    result: "4568",
    invocation_tool_name: "invoke_custom_tool",
    tool_call_run_id: "run_test_tool_2",
  }),
  event(3, "text_end", { text: "4568" }),
  event(4, "agent_end", { final_text: "4568" }),
];

const eventSummary = buildKeyTraceSummary(
  customToolEvents.map((trace) => ({
    id: trace.event_id,
    kind: "trace" as const,
    source: "initial_load" as const,
    sessionId: trace.session_id,
    receivedAt: trace.timestamp,
    event: trace,
  })),
);
assert(
  eventSummary.keyFlowToolResults.some(
    (result) =>
      result.invocationToolName === "invoke_custom_tool" &&
      result.toolName === "test_tool_2" &&
      result.resultText === "4568",
  ),
  "事件视图关键链路应直接显示 invoke_custom_tool -> test_tool_2 -> 4568",
);

const failedItems = buildTraceTimelineItems([
  {
    conversationId: "conv_failed_tool",
    sessionId: "ses_failed_tool",
    userMessage: null,
    events: [
      event(5, "tool_call_start", {
        tool_name: "test_tool_2",
        args: {},
        invocation_tool_name: "invoke_custom_tool",
        tool_call_run_id: "run_failed_tool",
      }),
      event(6, "error", {
        error: "未知扩展工具: test_tool_2。当前可用扩展工具: 无",
        phase: "agent_execution",
      }),
      event(7, "job_failed", {
        error: "未知扩展工具: test_tool_2。当前可用扩展工具: 无",
      }),
    ],
    status: "error",
    jobId: "job_failed_tool",
    pending: false,
    source: "messages",
  } satisfies ConversationView,
]);
assert(
  failedItems.some((item) => item.kind === "aggregated_tool" && item.failed),
  "失败时未完成工具应收束为失败工具卡",
);
assert(
  !failedItems.some(
    (item) => item.kind === "trace" && item.eventType === "tool_call_start",
  ),
  "失败时不应残留正在调用的 tool_call_start 卡片",
);
assert(
  failedItems.filter(
    (item) => item.kind === "trace" && ["error", "job_failed"].includes(item.eventType),
  ).length === 1,
  "相同错误不应重复显示两张执行异常卡",
);
