import { describe, expect, it } from "bun:test";
import { responsePartsToTimelineItems } from "./responseParts";

describe("responsePartsToTimelineItems", () => {
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
});
