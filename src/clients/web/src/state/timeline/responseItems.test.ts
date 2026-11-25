import { describe, expect, it } from "bun:test";
import {
  applyMessageStreamEvent,
  createMessageStreamState,
  messageStreamToResponseItems,
  type MessageStreamDataEvent,
  type MessageStreamEvent,
} from "../messageStream/index";
import { mergeCanonicalItems, responseItemsToTimelineItems } from "./responseItems";
import { deriveToolRowDisplay } from "../display/toolDisplay";
import type { TurnResponseItem } from "../../types/backend";

function streamEvent(
  eventSeq: number,
  type: Exclude<MessageStreamEvent["type"], "stream.snapshot">,
  payload: Record<string, unknown>,
): MessageStreamDataEvent {
  return { thread_id: "thr_00000000400070008000000000000002",
    event_id: `evt_${eventSeq}`,
    session_id: "ses_1",
    turn_id: "turn_1",
    turn_stream_id: "stream_1",
    event_seq: eventSeq,
    model_call_id: "stream-model-1",
    type,
    payload,
  };
}

describe("responseItemsToTimelineItems", () => {
  it("非文本详情不会被迟到摘要清空，未知输出保留原结构", () => {
    const detail: TurnResponseItem = {id: "card", kind: "content", projection: "detail", final: true,
      source: {item_id: "card", message_sequence: 1}, carrier_type: "custom_card",
      content: {type: "custom_card", data: {title: "原始结构"}}};
    const merged = mergeCanonicalItems([detail], [{...detail, projection: "summary", content: null}]);
    expect(merged[0]?.content).toEqual(detail.content);
    expect(responseItemsToTimelineItems(merged)[0]).toMatchObject({partKind: "content", content: detail.content,
      responseSources: [{id: "card", final: true}]});
  });

  it.each(["provider_id", "scoped_id"])("跨思考的 %s 工具结果合入原调用并保留独立来源", (carrier) => {
    const parts: TurnResponseItem[] = [
      {
        id: "call-before-reasoning",
        kind: "tool_call",
        projection: "detail",
        status: "failed",
        source: { message_sequence: 1, model_call_id: "model-1", assistant_message_sequence: 1, call_index: 0 },
        tool_call_id: "call-1",
        tool_name: "inspect_fixture",
        arguments: '{"path":"fixture.json"}',
      },
      {
        id: "reasoning-between",
        kind: "reasoning",
        projection: "detail",
        source: { message_sequence: 2, content_block_index: 0 },
        text: "等待工具结果",
      },
      {
        id: "result-after-reasoning",
        kind: "tool_result",
        projection: "detail",
        status: "completed",
        source: { message_sequence: 3, model_call_id: "model-1", result_message_sequence: 3 },
        tool_call_id: carrier === "scoped_id" ? "model-1:tool-call:call-1" : "call-1",
        result: "工具正文",
      },
      {
        id: "final-after-result",
        kind: "final_text",
        projection: "detail",
        source: { message_sequence: 4 },
        text: "最终响应",
      },
    ];
    const items = responseItemsToTimelineItems(parts);

    expect(items.map((item) => item.id)).toEqual([parts[0]!.id, parts[1]!.id, parts[3]!.id]);
    expect(items[0]?.responseSources).toEqual(
      [parts[0]!, parts[2]!].map(({ id, kind, source, projection, tool_call_id }) => ({ id, kind, source, projection, tool_call_id })),
    );
    expect(items[1]?.responseSources?.[0]?.id).toBe(parts[1]!.id);
    expect(parts.map(part => part.id)).toEqual([
      "call-before-reasoning", "reasoning-between", "result-after-reasoning", "final-after-result",
    ]);
    expect(items[0]).toMatchObject({
      kind: "aggregated_tool", toolName: "inspect_fixture", active: false, failed: true,
      resultText: "工具正文", detailsLoaded: true,
    });
  });

  it("并行工具交错回执各自合入原声明位置并保留失败和取消终态", () => {
    const parts: TurnResponseItem[] = [
      {
        id: "parallel-call-0", kind: "tool_call", projection: "detail", status: "running",
        source: { message_sequence: 1, assistant_message_sequence: 1, call_index: 0 },
        tool_call_id: "parallel-0", tool_name: "first", arguments: "{}",
      },
      {
        id: "parallel-call-1", kind: "tool_call", projection: "detail", status: "running",
        source: { message_sequence: 1, assistant_message_sequence: 1, call_index: 1 },
        tool_call_id: "parallel-1", tool_name: "second", arguments: "{}",
      },
      {
        id: "parallel-result-0", kind: "tool_result", projection: "detail", status: "failed",
        source: { message_sequence: 2, assistant_message_sequence: 1, call_index: 0, result_message_sequence: 2 },
        tool_call_id: "parallel-0", result: "失败详情",
      },
      {
        id: "parallel-result-1", kind: "tool_result", projection: "detail", status: "cancelled",
        source: { message_sequence: 3, assistant_message_sequence: 1, call_index: 1 },
        tool_call_id: "parallel-1", result: "调用取消", outcome_unknown: true,
      },
    ];
    const items = responseItemsToTimelineItems([parts[0]!, parts[1]!, parts[3]!, parts[2]!], { terminalFailure: true });

    expect(items.map(item => item.id)).toEqual(["parallel-call-0", "parallel-call-1"]);
    expect(items.map(item => item.responseSources?.map(source => source.id))).toEqual([
      ["parallel-call-0", "parallel-result-0"], ["parallel-call-1", "parallel-result-1"],
    ]);
    expect(items[0]).toMatchObject({ toolName: "first", resultText: "失败详情", active: false, failed: true, incomplete: false });
    expect(items[1]).toMatchObject({ toolName: "second", resultText: "调用取消", active: false, failed: false, incomplete: true, outcomeUnknown: true });
  });

  it("live SSE 的 exec_command 参数和结果贯通到终端展示", () => {
    let state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1", "stream_1", "test-workspace::ses_1", "thr_00000000400070008000000000000002"),
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

    const [item] = responseItemsToTimelineItems(messageStreamToResponseItems(state));
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
    const display = deriveToolRowDisplay(item, true);
    expect(display.previewText).toBe("命令已完成，终端仍可打开");
    expect(display.detailsContent?.result).toContain("/workspace");
  });

  it("历史 detail response parts 使用 arguments/result 展示 exec_command", () => {
    const [item] = responseItemsToTimelineItems([
      {
        id: "tool-call:detail",
        kind: "tool_call",
        projection: "detail",
        status: "completed",
        source: { message_sequence: 1, assistant_message_sequence: 1, call_index: 0 },
        tool_call_id: "call-detail",
        tool_name: "exec_command",
        arguments: JSON.stringify({ cmd: "ls", path: "." }),
      },
      {
        id: "tool-result:detail",
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
    const display = deriveToolRowDisplay(item, true);
    expect(display.detailsContent?.input).toContain("ls");
    expect(display.detailsContent?.result).toContain("README.md");
    expect(display.previewText).toBe("命令已完成，终端仍可打开");
  });

  it("历史 summary 没有正文时保留 tool_call_id，供 ToolRow 加载 detail", () => {
    const [item] = responseItemsToTimelineItems([
      {
        id: "tool-call:summary",
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
    expect(item.declared).toBe(true);
    expect(deriveToolRowDisplay(item, false).previewText).toBe("尚无执行状态");
  });

  it("按统一语义模型渲染历史 content、工具和最终文本", () => {
    const items = responseItemsToTimelineItems([
      {
        id: "reasoning:1:0",
        kind: "reasoning",
        projection: "detail",
        source: { message_sequence: 1, content_block_index: 0 },
        text: "先分析",
      },
      {
        id: "tool-call:call-1",
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
        id: "tool-result:call-1",
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
        id: "message:3:content:0",
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
    const items = responseItemsToTimelineItems([
      {
        id: "tool-call:1:0",
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
        id: "tool-call:2:0",
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
        id: "tool-call:3:0",
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
        id: "tool-call:4:0",
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

  it("结果缺少调用归属时不因本页唯一候选猜配", () => {
    const items = responseItemsToTimelineItems([
      {
        id: "tool-call:running",
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
        id: "tool-result:completed",
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

    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({
      kind: "aggregated_tool",
      active: false,
      declared: true,
      resultText: "",
    });
    expect(items[1]).toMatchObject({ kind: "aggregated_tool", resultText: "ok", declared: false });
  });

  it("终态失败的历史工具没有结果时显示 outcome_unknown", () => {
    const items = responseItemsToTimelineItems([
      {
        id: "tool-call:unknown",
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
    const items = responseItemsToTimelineItems([
      {
        id: "tool-call:cancelled",
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
    const items = responseItemsToTimelineItems([
      {
        id: "reasoning:detail",
        kind: "reasoning",
        projection: "streaming",
        source: { message_sequence: 1, content_block_index: 0 },
        text: "明文思考",
        carrier_type: "thinking",
      },
      {
        id: "reasoning:summary",
        kind: "reasoning_summary",
        projection: "streaming",
        source: { message_sequence: 2, content_block_index: 0 },
        text: "摘要思考",
        carrier_type: "reasoning_items",
      },
      {
        id: "reasoning:encrypted",
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

  it("不同ModelCall复用调用ID和index时只结算自己的声明", () => {
    const items = responseItemsToTimelineItems([
      { id: "a", kind: "tool_call", projection: "summary", status: "completed",
        tool_call_id: "reused", tool_name: "read", source: { message_sequence: 0, call_index: 0, model_call_id: "m1" } },
      { id: "b", kind: "tool_call", projection: "summary", status: "completed",
        tool_call_id: "reused", tool_name: "read", source: { message_sequence: 0, call_index: 0, model_call_id: "m2" } },
      { id: "rb", kind: "tool_result", projection: "detail", status: "failed", result: "B失败",
        tool_call_id: "m2:tool-call:reused", source: { message_sequence: 0, model_call_id: "m2" } },
    ]);
    expect(items.map(item => item.id)).toEqual(["a", "b"]);
    expect(items[0]).toMatchObject({ declared: true, active: false, failed: false, resultText: "" });
    expect(items[1]).toMatchObject({ declared: false, active: false, failed: true, resultText: "B失败" });
    expect(items[1]?.responseSources?.map(source => source.id)).toEqual(["b", "rb"]);
  });

  it("跨页只取得调用时消费真实执行摘要，不以声明completed显示成功", () => {
    const execution = { session_id: "s", thread_id: "t", turn_stream_id: "stream", tool_invocation_id: "inv",
      tool_attempt_id: "attempt", tool_execution_id: "attempt", started_seq: 1, started_at: "2026-11-23T00:00:00Z",
      execution_completed_at: undefined };
    for (const [status, outcome, expected] of [
      ["running", undefined, { active: true, failed: false, outcomeUnknown: false }],
      ["completed", "provider_error", { active: false, failed: true, outcomeUnknown: false }],
      ["completed", "outcome_unknown", { active: false, failed: false, outcomeUnknown: true }],
      ["completed", "success", { active: false, failed: false, outcomeUnknown: false }],
    ] as const) {
      const [item] = responseItemsToTimelineItems([{ id: "a", kind: "tool_call", projection: "summary",
        status: status === "running" ? "completed" : "failed", outcome_unknown: status !== "running",
        source: { message_sequence: 0, model_call_id: "m" }, tool_call_id: "a",
        tool_execution: { ...execution, status, outcome } }]);
      expect(item).toMatchObject({ ...expected, declared: false });
    }
  });

  it.each([
    ["provider_error", "failure", { active: false, failed: true }],
    ["cancelled", "cancelled", { active: false, failed: false, incomplete: true }],
  ] as const)("同绑定新摘要保留正文及%s终态，不被迟到摘要抹掉", (outcome, toolOutcome, expected) => {
    const detail: TurnResponseItem = { id: "a", kind: "tool_call", projection: "detail", status: "completed",
      source: { message_sequence: 0, item_id: "a", logical_item_ordinal: 1, model_call_id: "m" },
      tool_call_id: "a", arguments: '{"path":"A"}', text: "已加载正文", truncated: true };
    const execution = { session_id: "s", thread_id: "t", turn_stream_id: "stream", tool_invocation_id: "inv",
      tool_attempt_id: "attempt", tool_execution_id: "attempt", started_seq: 1, started_at: "2026-11-23T00:00:00Z",
      execution_completed_at: "2026-11-23T00:00:01Z", status: "completed", outcome };
    const summary: TurnResponseItem = { ...detail, projection: "summary", text: "", arguments: null,
      tool_execution: execution, tool_outcome: toolOutcome, truncated: false };
    const merged = mergeCanonicalItems([detail], [summary]);
    expect(merged[0]).toMatchObject({ projection: "detail", arguments: detail.arguments, text: detail.text,
      truncated: true, tool_execution: execution });
    for (const tool_execution of [null,
      { ...execution, status: "running", outcome: undefined, execution_completed_at: undefined },
      { ...execution, outcome: undefined, execution_completed_at: undefined },
      { ...execution, outcome: "outcome_unknown", execution_completed_at: undefined }]) {
      const delayed = mergeCanonicalItems(merged, [{ ...summary, tool_execution, tool_outcome: null }]);
      expect(delayed[0]?.tool_execution).toEqual(execution);
      expect(delayed[0]?.tool_outcome).toBe(toolOutcome);
      expect(responseItemsToTimelineItems(delayed)[0]).toMatchObject(expected);
    }
    expect(detail.tool_execution).toBeUndefined();
    expect(summary.arguments).toBeNull();
  });

  it("同一执行的终态摘要缺开始时间时仍保留已知边界，不丢状态", () => {
    const running: TurnResponseItem = { id: "a", kind: "tool_call", projection: "summary", status: "completed",
      source: { message_sequence: 0, item_id: "a", logical_item_ordinal: 1, model_call_id: "m" },
      tool_call_id: "a", tool_execution: { session_id: "s", thread_id: "t", turn_stream_id: "stream",
        tool_invocation_id: "inv", tool_attempt_id: "attempt", tool_execution_id: "attempt", started_seq: 1,
        started_at: "2026-11-23T00:00:00Z", execution_completed_at: undefined, status: "running" } };
    const completed: TurnResponseItem = { ...running, tool_outcome: "success",
      tool_execution: { ...running.tool_execution!, started_at: undefined, status: "completed", outcome: "success",
        execution_completed_at: "2026-11-23T00:00:01Z" } };
    expect(responseItemsToTimelineItems([{ ...running,
      tool_execution: { ...running.tool_execution!, started_at: undefined } }])[0]).toMatchObject({ active: true, declared: false });
    const merged = mergeCanonicalItems([running], [completed]);
    expect(merged[0]?.tool_execution).toMatchObject({ status: "completed", outcome: "success",
      started_at: running.tool_execution!.started_at,
      execution_completed_at: completed.tool_execution!.execution_completed_at });
    expect(responseItemsToTimelineItems(merged)[0]).toMatchObject({ active: false, declared: false, failed: false });
    const recovered = mergeCanonicalItems([completed], [running]);
    expect(recovered[0]?.tool_execution).toEqual(merged[0]?.tool_execution);
    expect(recovered[0]?.tool_outcome).toBe("success");
    expect(responseItemsToTimelineItems(recovered)[0]).toMatchObject({ active: false, declared: false, failed: false });
    expect(completed.tool_execution?.started_at).toBeUndefined();
  });

  it("回执marker独立于执行时序，跨页调用与独立回执均保留真实结果", () => {
    for (const kind of ["tool_call", "tool_result"] as const) {
      for (const [tool_outcome, expected] of [
        ["success", { failed: false, incomplete: false, outcomeUnknown: false }],
        ["failure", { failed: true, incomplete: false, outcomeUnknown: false }],
        ["cancelled", { failed: false, incomplete: true, outcomeUnknown: false }],
        ["unknown", { failed: false, incomplete: false, outcomeUnknown: true }],
      ] as const) {
        const part: TurnResponseItem = { id: "a", kind, projection: "summary", status: "completed",
          source: { message_sequence: 1, item_id: "a", model_call_id: "m" },
          tool_call_id: "a", tool_outcome, tool_execution: null };
        const [item] = responseItemsToTimelineItems([part]);
        expect(item).toMatchObject({ ...expected, active: false, declared: false });
        const delayed = mergeCanonicalItems([part], [{ ...part, tool_outcome: null }]);
        expect(delayed[0]?.tool_outcome).toBe(tool_outcome);
      }
    }
  });
});
