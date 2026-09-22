import { describe, expect, it } from "bun:test";
import {
  applyMessageStreamEvent,
  createMessageStreamState,
  messageStreamToResponseParts,
  type MessageStreamDataEvent,
  type MessageStreamEvent,
} from "../messageStream/index";
import { responsePartsToTimelineItems } from "./responseParts";
import { formatToolCardContent, toolCollapsedText } from "../toolDisplay";

function streamEvent(
  eventSeq: number,
  type: Exclude<MessageStreamEvent["type"], "stream.snapshot">,
  payload: Record<string, unknown>,
): MessageStreamDataEvent {
  return {
    event_id: `evt_${eventSeq}`,
    session_id: "ses_1",
    turn_id: "turn_1",
    turn_stream_id: "stream_1",
    event_seq: eventSeq,
    type,
    payload,
  };
}

describe("responsePartsToTimelineItems", () => {
  it("live SSE 的 exec_command 参数和结果贯通到终端展示", () => {
    let state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1", "stream_1"),
      streamEvent(1, "tool_call", {
        tool_call_id: "call-exec",
        tool_name: "exec_command",
        arguments: { cmd: "pwd", yield_time_ms: 10000 },
      }),
    );
    state = applyMessageStreamEvent(state, streamEvent(2, "tool.started", {
      tool_execution_id: "exec-1",
      tool_call_id: "call-exec",
      tool_name: "exec_command",
    }));
    state = applyMessageStreamEvent(state, streamEvent(3, "tool.completed", {
      tool_execution_id: "exec-1",
      tool_call_id: "call-exec",
      tool_name: "exec_command",
      status: "completed",
      result: JSON.stringify({
        chunk_id: "term-1",
        output: "/workspace",
        exit_code: 0,
        status: "success",
      }),
    }));

    const [item] = responsePartsToTimelineItems(messageStreamToResponseParts(state));
    expect(item).toMatchObject({
      kind: "aggregated_tool",
      toolName: "exec_command",
      inputText: JSON.stringify({ cmd: "pwd", yield_time_ms: 10000 }),
      resultText: JSON.stringify({
        chunk_id: "term-1",
        output: "/workspace",
        exit_code: 0,
        status: "success",
      }),
    });
    if (item?.kind !== "aggregated_tool") throw new Error("应生成工具时间线项");
    expect(toolCollapsedText(item)).toBe("命令已完成，终端仍可打开");
    expect(formatToolCardContent(item)).toContain("/workspace");
  });

  it("历史 detail response parts 使用 arguments/result 展示 exec_command", () => {
    const [item] = responsePartsToTimelineItems([
      {
        part_id: "tool-call:detail",
        kind: "tool_call",
        projection: "detail",
        status: "completed",
        source: { message_sequence: 1, assistant_message_sequence: 1, call_index: 0 },
        tool_call_id: "call-detail",
        tool_name: "exec_command",
        arguments: JSON.stringify({ cmd: "ls", path: "." }),
      },
      {
        part_id: "tool-result:detail",
        kind: "tool_result",
        projection: "detail",
        status: "completed",
        source: { message_sequence: 2, assistant_message_sequence: 1, call_index: 0 },
        tool_call_id: "call-detail",
        result: JSON.stringify({ output: "README.md", exit_code: 0 }),
      },
    ]);

    expect(item?.kind).toBe("aggregated_tool");
    if (item?.kind !== "aggregated_tool") throw new Error("应生成工具时间线项");
    expect(formatToolCardContent(item)).toContain("ls");
    expect(formatToolCardContent(item)).toContain("README.md");
    expect(toolCollapsedText(item)).toBe("命令已完成，终端仍可打开");
  });

  it("历史 summary 没有正文时保留 tool_call_id，供 ToolRow 加载 detail", () => {
    const [item] = responsePartsToTimelineItems([
      {
        part_id: "tool-call:summary",
        kind: "tool_call",
        projection: "summary",
        status: "completed",
        source: { message_sequence: 1, assistant_message_sequence: 1, call_index: 0 },
        tool_call_id: "call-summary",
        tool_name: "exec_command",
        arguments: null,
      },
    ]);

    expect(item).toMatchObject({
      kind: "aggregated_tool",
      toolCallId: "call-summary",
      inputText: "",
      resultText: "",
      detailsLoaded: false,
    });
    if (item?.kind !== "aggregated_tool") throw new Error("应生成工具时间线项");
    expect(toolCollapsedText(item)).toBe("命令工具已返回，终端仍可打开");
  });

  it("按统一语义模型渲染历史 content、工具和最终文本", () => {
    const items = responsePartsToTimelineItems([
      {
        part_id: "reasoning:1:0",
        kind: "reasoning",
        projection: "detail",
        source: { message_sequence: 1, content_block_index: 0 },
        text: "先分析",
      },
      {
        part_id: "tool-call:call-1",
        kind: "tool_call",
        projection: "detail",
        status: "pending",
        source: {
          message_sequence: 1,
          assistant_message_sequence: 1,
          call_index: 0,
        },
        tool_call_id: "call-1",
        tool_name: "inspect_fixture",
        arguments: '{"path":"fixture/1.json"}',
      },
      {
        part_id: "tool-result:call-1",
        kind: "tool_result",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 2,
          assistant_message_sequence: 1,
          call_index: 0,
          result_message_sequence: 2,
        },
        tool_call_id: "call-1",
        result: "ok",
        text: "ok",
      },
      {
        part_id: "message:3:content:0",
        kind: "final_text",
        projection: "detail",
        source: { message_sequence: 3, content_block_index: 0 },
        text: "完成",
        final: true,
      },
    ]);

    expect(items.map((item) => item.kind)).toEqual([
      "aggregated_text",
      "aggregated_tool",
      "aggregated_text",
    ]);
    expect(items[1]).toMatchObject({
      kind: "aggregated_tool",
      toolName: "inspect_fixture",
      inputText: '{"path":"fixture/1.json"}',
      resultText: "ok",
      active: false,
    });
  });

  it("不同 assistant 复用 tool_call_id 时仍按来源坐标分别合并", () => {
    const items = responsePartsToTimelineItems([
      {
        part_id: "tool-call:1:0",
        kind: "tool_call",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 1,
          assistant_message_sequence: 1,
          call_index: 0,
        },
        tool_call_id: "reused",
        tool_name: "first",
        arguments: "{}",
      },
      {
        part_id: "tool-call:2:0",
        kind: "tool_result",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 2,
          assistant_message_sequence: 1,
          call_index: 0,
          result_message_sequence: 2,
        },
        tool_call_id: "reused",
        result: "first result",
        text: "first result",
      },
      {
        part_id: "tool-call:3:0",
        kind: "tool_call",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 3,
          assistant_message_sequence: 3,
          call_index: 0,
        },
        tool_call_id: "reused",
        tool_name: "second",
        arguments: "{}",
      },
      {
        part_id: "tool-call:4:0",
        kind: "tool_result",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 4,
          assistant_message_sequence: 3,
          call_index: 0,
          result_message_sequence: 4,
        },
        tool_call_id: "reused",
        result: "second result",
        text: "second result",
      },
    ]);

    expect(items).toHaveLength(2);
    expect(items.map((item) => item.kind)).toEqual([
      "aggregated_tool",
      "aggregated_tool",
    ]);
    expect(items.map((item) => item.kind === "aggregated_tool" && item.resultText)).toEqual([
      "first result",
      "second result",
    ]);
  });

  it("工具结果只有 tool_call_id 时也能把历史 tool_call 标记为完成", () => {
    const items = responsePartsToTimelineItems([
      {
        part_id: "tool-call:running",
        kind: "tool_call",
        projection: "detail",
        status: "running",
        source: {
          message_sequence: 10,
          assistant_message_sequence: 10,
          call_index: 0,
        },
        tool_call_id: "call-running",
        tool_name: "inspect_fixture",
        arguments: "{\"path\":\"fixture/128.json\"}",
      },
      {
        part_id: "tool-result:completed",
        kind: "tool_result",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 11,
          call_index: 0,
        },
        tool_call_id: "call-running",
        result: "ok",
        text: "ok",
      },
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "aggregated_tool",
      active: false,
      resultText: "ok",
    });
  });

  it("终态失败的历史工具没有结果时显示 outcome_unknown", () => {
    const items = responsePartsToTimelineItems([
      {
        part_id: "tool-call:unknown",
        kind: "tool_call",
        projection: "detail",
        status: "running",
        source: {
          message_sequence: 20,
          assistant_message_sequence: 20,
          call_index: 0,
        },
        tool_call_id: "call-unknown",
        tool_name: "python_exec",
        arguments: "{\"timeout_seconds\":60}",
      },
    ], { terminalFailure: true });

    expect(items[0]).toMatchObject({
      kind: "aggregated_tool",
      active: false,
      failed: true,
      outcomeUnknown: true,
    });
  });

  it("取消终态的未完成工具显示为调用未完成而不是结果未知", () => {
    const items = responsePartsToTimelineItems([
      {
        part_id: "tool-call:cancelled",
        kind: "tool_call",
        projection: "summary",
        status: "failed",
        source: {
          message_sequence: 2,
          assistant_message_sequence: 2,
          call_index: 0,
        },
        tool_call_id: "call-cancelled",
        tool_name: "read_file",
        text: "pending",
        outcome_unknown: true,
      },
    ], { terminalCancellation: true });

    expect(items[0]).toMatchObject({
      kind: "aggregated_tool",
      active: false,
      incomplete: true,
      failed: false,
      outcomeUnknown: false,
    });
  });

  it("保留详细思考、summary 和加密思考的语义边界，同时使用统一文本展示模型", () => {
    const items = responsePartsToTimelineItems([
      {
        part_id: "reasoning:detail",
        kind: "reasoning",
        projection: "streaming",
        source: { message_sequence: 1, content_block_index: 0 },
        text: "明文思考",
        carrier_type: "thinking",
      },
      {
        part_id: "reasoning:summary",
        kind: "reasoning_summary",
        projection: "streaming",
        source: { message_sequence: 2, content_block_index: 0 },
        text: "摘要思考",
        carrier_type: "reasoning_items",
      },
      {
        part_id: "reasoning:encrypted",
        kind: "reasoning_encrypted",
        projection: "summary",
        source: { message_sequence: 3, content_block_index: 0 },
        text: "",
        carrier_type: "redacted_thinking",
      },
    ]);

    expect(items).toHaveLength(3);
    expect(items.every((item) => item.kind === "aggregated_text")).toBe(true);
    expect(items.map((item) => item.kind === "aggregated_text" && item.partKind)).toEqual([
      "reasoning",
      "reasoning",
      "reasoning",
    ]);
    expect(items.map((item) => item.kind === "aggregated_text" && item.reasoningKind)).toEqual([
      "reasoning",
      "reasoning_summary",
      "reasoning_encrypted",
    ]);
    expect(items[2]).toMatchObject({ redacted: true });
  });
});
