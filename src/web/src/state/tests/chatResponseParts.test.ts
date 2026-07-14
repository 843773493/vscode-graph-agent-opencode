import type { TraceEvent } from "../../types/backend";
import { compactWorkMarkdown } from "../../components/chat/ThinkingSection";
import { aggregateConversationEvents } from "../trace/traceAggregation";
import { buildTraceEvent } from "../traceEvents";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function event(
  index: number,
  type: TraceEvent["type"],
  payload: Record<string, unknown>,
  partId: string | null = null,
): TraceEvent {
  return {
    event_id: `evt_part_${index}`,
    part_id: partId,
    session_id: "session_part",
    job_id: "job_part",
    type,
    timestamp: `2026-07-11T00:00:0${index}.000Z`,
    payload,
    raw: { payload },
  };
}

assert(
  compactWorkMarkdown(
    "G\n\nrep\n\n \n\n已\n\n恢复\n\n \n\nmatched\n\n。\n\n现在\n\n读取\n\n命中\n\n行\n\n附近\n\n（\n\n行\n\n3-\n\n4\n\n）\n\n。",
  ) === "Grep 已恢复 matched。现在读取命中行附近（行3-4）。",
  "工作组应合并模型按 token 插入的异常空段落，同时保留显式空格",
);

const streamingEvents = [
  event(1, "text_start", { kind: "reasoning" }, "reasoning_1"),
  event(2, "text_delta", { kind: "reasoning", text: "正在分析" }, "reasoning_1"),
  event(3, "tool_call_start", {
    tool_name: "read_file",
    args: { file_path: "README.md" },
  }, "tool_1"),
];

const streamingParts = aggregateConversationEvents(
  streamingEvents,
  "conversation_part",
  true,
);
const activeTool = streamingParts.find((item) => item.kind === "aggregated_tool");
assert(activeTool?.active === true, "运行中工具应原地保留 active 状态");
assert(
  streamingParts.filter((item) => item.kind === "aggregated_tool").length === 1,
  "tool start 不应生成额外生命周期卡片",
);

const completedParts = aggregateConversationEvents(
  [
    ...streamingEvents,
    event(4, "tool_call_end", {
      tool_name: "read_file",
      result: "# README",
    }, "tool_1"),
    event(5, "text_start", { kind: "markdown" }, "markdown_1"),
    event(6, "text_delta", { kind: "markdown", text: "已完成。" }, "markdown_1"),
    event(7, "text_end", { kind: "markdown", text: "已完成。" }, "markdown_1"),
  ],
  "conversation_part",
  false,
);
const completedTool = completedParts.find((item) => item.kind === "aggregated_tool");
assert(completedTool?.active === false, "完成工具刷新恢复后不得继续显示 spinner");
assert(completedTool?.resultText === "# README", "刷新恢复应保留工具输出");

const failedResultParts = aggregateConversationEvents(
  [
    event(8, "tool_call_start", {
      tool_name: "read_file",
      args: { file_path: "/missing.md" },
    }, "tool_error"),
    event(9, "tool_call_end", {
      tool_name: "read_file",
      result: "Error: File /missing.md not found",
    }, "tool_error"),
  ],
  "conversation_part",
  false,
);
const failedResultTool = failedResultParts.find((item) => item.kind === "aggregated_tool");
assert(failedResultTool?.failed === true, "Error: 工具结果必须显示为失败状态");

const incompleteHistoryParts = aggregateConversationEvents(
  streamingEvents,
  "conversation_part",
  false,
);
const incompleteTool = incompleteHistoryParts.find((item) => item.kind === "aggregated_tool");
assert(incompleteTool?.failed === true, "已结束历史中缺失 tool end 必须显式标记失败");
assert(incompleteTool?.active === false, "不完整历史不得恢复为运行中");

const orderedParts = aggregateConversationEvents(
  [
    event(10, "text_start", { kind: "markdown" }, "markdown_before"),
    event(11, "text_delta", { kind: "markdown", text: "先说明。\n\n" }, "markdown_before"),
    event(12, "text_end", { kind: "markdown", text: "先说明。\n\n" }, "markdown_before"),
    event(13, "tool_call_start", { tool_name: "read_file", args: {} }, "tool_middle"),
    event(14, "tool_call_end", { tool_name: "read_file", result: "ok" }, "tool_middle"),
    event(15, "text_start", { kind: "markdown" }, "markdown_after"),
    event(16, "text_delta", { kind: "markdown", text: "再总结。" }, "markdown_after"),
    event(17, "text_end", { kind: "markdown", text: "再总结。" }, "markdown_after"),
  ],
  "conversation_order",
  false,
);
assert(
  orderedParts.filter((item) => item.kind !== "skill_summary").map((item) => item.id).join(",") ===
    "markdown_before,tool_middle,markdown_after",
  "part 必须保持首次出现顺序，工具完成不得移动到末尾",
);
const firstMarkdown = orderedParts.find((item) => item.id === "markdown_before");
assert(
  firstMarkdown?.kind === "aggregated_text" && firstMarkdown.text === "先说明。\n\n",
  "Markdown 中的连续换行必须原样保留",
);

const streamedPart = buildTraceEvent({
  event_id: "evt_stream_part",
  part_id: "part_stream",
  session_id: "session_part",
  job_id: "job_part",
  step_id: null,
  agent_id: "default",
  timestamp: "2026-07-11T00:00:00.000Z",
  type: "text_start",
  payload: { kind: "markdown" },
});
assert(streamedPart.part_id === "part_stream", "SSE 适配层必须保留顶层 part_id");
