import { describe, expect, test } from "bun:test";

import { createMessageStreamState } from "./state";
import { messageStreamToResponseParts } from "./responseProjection";

describe("消息流响应投影排序", () => {
  test("乱序执行仍优先保留 canonical 工具并按生命周期合并排序", () => {
    const state = createMessageStreamState(
      "ses_1",
      "turn_1",
      "strm_1",
      "workspace::ses_1",
      "thr_1",
    );
    state.toolCalls.call_1 = {
      tool_name: "read_file",
      arguments: { path: "README.md" },
    };
    state.toolExecutions = [
      {
        tool_execution_id: "exec_reconciled",
        tool_call_id: "call_1",
        tool_name: "read_file",
        status: "completed",
        completion_reason: "reconciled_tool_message",
        result: "重复结果",
        started_seq: 4,
      },
      {
        tool_execution_id: "exec_canonical",
        tool_call_id: "call_1",
        tool_name: "read_file",
        status: "completed",
        completion_reason: "tool_completed",
        result: "权威结果",
        started_seq: 2,
      },
    ];
    state.blocks = [{
      block_id: "block_after_tool",
      model_call_id: null,
      block_index: 0,
      carrier_type: "text",
      status: "completed",
      text: "工具之后的正文",
      items: [],
      redacted: false,
      projection: "streaming",
      started_seq: 3,
    }];

    const parts = messageStreamToResponseParts(state);

    expect(parts.map((part) => part.part_id)).toEqual([
      "exec_canonical",
      "exec_canonical:result",
      "block_after_tool",
    ]);
    expect(parts[1]?.text).toBe("权威结果");
  });

  test("同优先级执行按生命周期序号和 execution id 稳定选择", () => {
    const state = createMessageStreamState(
      "ses_1",
      "turn_1",
      "strm_1",
      "workspace::ses_1",
      "thr_1",
    );
    state.toolExecutions = [
      {
        tool_execution_id: "exec_later",
        tool_call_id: "call_1",
        tool_name: "shell",
        status: "completed",
        result: "较晚",
        started_seq: 8,
      },
      {
        tool_execution_id: "exec_z",
        tool_call_id: "call_1",
        tool_name: "shell",
        status: "completed",
        result: "同序号但 id 较大",
        started_seq: 4,
      },
      {
        tool_execution_id: "exec_a",
        tool_call_id: "call_1",
        tool_name: "shell",
        status: "completed",
        result: "同序号且 id 较小",
        started_seq: 4,
      },
    ];

    const parts = messageStreamToResponseParts(state);

    expect(parts.map((part) => part.part_id)).toEqual([
      "exec_a",
      "exec_a:result",
    ]);
    expect(parts[1]?.text).toBe("同序号且 id 较小");
  });
});
