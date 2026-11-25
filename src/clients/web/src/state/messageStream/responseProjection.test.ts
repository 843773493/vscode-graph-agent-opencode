import { describe, expect, test } from "bun:test";

import { createMessageStreamState } from "./state";
import { messageStreamToResponseItems } from "./responseProjection";
import { applyMessageStreamEvent } from "./eventReducer";
import { applySnapshotState } from "./snapshotHydration";
import { responseItemsToTimelineItems } from "../timeline/responseItems";
import type { MessageStreamDataEvent } from "./types";

describe("消息流响应投影排序", () => {
  test("媒体增量和快照保留原块，块完成不会冒充最终回答", () => {
    const identity = { session_id: "ses_1", thread_id: "thr_1", turn_id: "turn_1", turn_stream_id: "strm_1" };
    const image = { type: "image_url", image_url: { url: "https://example.test/image.png" } };
    let state = createMessageStreamState("ses_1", "turn_1", "strm_1", "workspace::ses_1", "thr_1");
    state = applyMessageStreamEvent(state, { ...identity, type: "block.delta", event_seq: 1,
      event_id: "image-delta", model_call_id: "m", payload: { block_id: "image", block_index: 0,
        carrier_type: "image_url", operation: "item_upsert", item: image } });
    state = applyMessageStreamEvent(state, { ...identity, type: "block.completed", event_seq: 2,
      event_id: "image-completed", model_call_id: "m", payload: { block_id: "image" } });
    const refreshed = applySnapshotState(state, { snapshot_seq: 2, stream_status: "open",
      agent_loop_status: "running", current_attempt: 1, blocks: state.blocks.map(block => ({...block,
        model_call_id: block.model_call_id ?? undefined, items: [image]})),
      tool_executions: [], tool_calls: [], model_calls: [], activities: [], resource_refs: [], resumable: false }, identity);
    for (const source of [state, refreshed]) {
      const parts = messageStreamToResponseItems(source);
      expect(parts).toHaveLength(1);
      expect(parts[0]).toMatchObject({ id: "image", kind: "content", content: image, final: false });
      expect(responseItemsToTimelineItems(parts)[0]).toMatchObject({ partKind: "content",
        media: {kind: "image", source: image.image_url.url} });
    }
  });

  test("已确认取消在增量和快照中保留，工具行不显示执行成功", () => {
    let state = createMessageStreamState("ses_1", "turn_1", "strm_1", "workspace::ses_1", "thr_1");
    const identity = { session_id: "ses_1", thread_id: "thr_1", turn_id: "turn_1", turn_stream_id: "strm_1" };
    let sequence = 0;
    const emit = (type: MessageStreamDataEvent["type"], payload: Record<string, unknown>) => {
      sequence += 1;
      state = applyMessageStreamEvent(state, { ...identity, type, payload, model_call_id: "model-1",
        event_seq: sequence, event_id: `event-${sequence}` });
    };
    emit("tool_call.delta", { tool_call_id: "a", tool_name: "read", provider_call_index: 0,
      arguments: {}, arguments_complete: true });
    emit("tool.started", { tool_call_id: "a", tool_name: "read", tool_execution_id: "attempt-a" });
    emit("tool.completed", { tool_call_id: "a", tool_name: "read", tool_execution_id: "attempt-a",
      status: "completed", outcome: "cancelled", result: "已取消" });
    const refreshed = applySnapshotState(state, {
      snapshot_seq: sequence, stream_status: "open", agent_loop_status: "running", current_attempt: 1,
      blocks: [], tool_executions: state.toolExecutions,
      tool_calls: [{ ...state.toolCalls.a, tool_call_id: "a", tool_name: "read" }],
      model_calls: [], activities: [], resource_refs: [], resumable: false,
    }, identity);
    for (const source of [state, refreshed]) {
      expect(source.toolExecutions[0].outcome).toBe("cancelled");
      const parts = messageStreamToResponseItems(source);
      expect(parts.map(part => part.status)).toEqual(["cancelled", "cancelled"]);
      const tools = responseItemsToTimelineItems(parts).filter(item => item.kind === "aggregated_tool");
      expect(tools).toHaveLength(1);
      expect(tools[0]).toMatchObject({ active: false, failed: false, incomplete: true, outcomeUnknown: false });
    }
  });

  test("工具片段反序到达，声明顺序跨执行与快照恢复保持稳定", () => {
    let state = createMessageStreamState("ses_1", "turn_1", "strm_1", "workspace::ses_1", "thr_1");
    const identity = { session_id: "ses_1", thread_id: "thr_1", turn_id: "turn_1", turn_stream_id: "strm_1" };
    let sequence = 0;
    const emit = (type: MessageStreamDataEvent["type"], payload: Record<string, unknown>) => {
      sequence += 1;
      state = applyMessageStreamEvent(state, { ...identity, type, payload, model_call_id: "model-1",
        event_seq: sequence, event_id: `event-${sequence}` });
    };
    emit("tool_call.delta", { tool_call_id: "b", tool_name: "read", provider_call_index: 1,
      arguments: { paths: ["B"] }, arguments_complete: true });
    emit("block.delta", { block_id: "text", block_index: 0, carrier_type: "text", text: "正文", operation: "append" });
    emit("tool_call.delta", { tool_call_id: "a", tool_name: "read", provider_call_index: 0,
      arguments_delta: '{"paths":["A"', arguments_complete: false });
    emit("tool.started", { tool_call_id: "b", tool_name: "read", tool_execution_id: "attempt-b" });
    emit("tool.completed", { tool_call_id: "b", tool_name: "read", tool_execution_id: "attempt-b",
      status: "completed", outcome: "success", result: "B" });
    const parts = messageStreamToResponseItems(state);
    expect(parts.map(part => part.id)).toEqual(["a", "text", "b", "b:result"]);
    expect(parts.filter(part => part.kind === "tool_call").map(part => [part.tool_call_id, part.status]))
      .toEqual([["a", "running"], ["b", "completed"]]);
    const refreshed = applySnapshotState(state, {
      snapshot_seq: sequence, stream_status: "open", agent_loop_status: "running", current_attempt: 1,
      blocks: state.blocks.map(block => ({ ...block, items: [], model_call_id: block.model_call_id ?? undefined })), tool_executions: state.toolExecutions,
      tool_calls: ["b", "a"].map(id => ({ ...state.toolCalls[id], tool_call_id: id, tool_name: "read" })),
      model_calls: [], activities: [], resource_refs: [], resumable: false,
    }, identity);
    expect(messageStreamToResponseItems(refreshed)).toEqual(parts);
  });

  test("反序启动与完成保留声明顺序，刷新保留真实完成观察", () => {
    let state = createMessageStreamState("ses_1", "turn_1", "strm_1", "workspace::ses_1", "thr_1");
    const identity = { session_id: "ses_1", thread_id: "thr_1", turn_id: "turn_1", turn_stream_id: "strm_1" };
    let sequence = 0;
    const emit = (type: MessageStreamDataEvent["type"], payload: Record<string, unknown>) => {
      sequence += 1;
      state = applyMessageStreamEvent(state, { ...identity, type, payload, event_seq: sequence,
        model_call_id: "model-1", event_id: `event-${sequence}`, emitted_at: `2026-11-23T00:00:0${sequence}Z` });
    };
    for (const call of ["a", "b"]) emit("tool_call", {
      tool_call_id: call, tool_name: "read", arguments_complete: true, status: "completed",
    });
    expect(messageStreamToResponseItems(state).map(part => [part.tool_call_id, part.status]))
      .toEqual([["a", "pending"], ["b", "pending"]]);
    expect(responseItemsToTimelineItems(messageStreamToResponseItems(state)).map(item =>
      item.kind === "aggregated_tool" && { id: item.id, declared: item.declared, active: item.active }))
      .toEqual([{ id: "a", declared: true, active: false }, { id: "b", declared: true, active: false }]);
    const declarationIds = messageStreamToResponseItems(state).map(part => part.id);
    for (const call of ["b", "a"]) emit("tool.started", {
      tool_call_id: call, tool_execution_id: `attempt-${call}`, tool_attempt_id: `attempt-${call}`,
      tool_invocation_id: `invocation-${call}`, tool_name: "read",
    });
    emit("tool.completed", { tool_call_id: "b", tool_execution_id: "attempt-b", tool_attempt_id: "attempt-b",
      tool_invocation_id: "invocation-b", tool_name: "read", status: "completed", outcome: "success",
      execution_completed_at: "2026-11-23T00:00:04.500Z" });
    const parts = messageStreamToResponseItems(state);
    expect(parts.filter(part => part.kind === "tool_call").map(part => part.id)).toEqual(declarationIds);
    expect(parts.filter(part => part.kind === "tool_call").map(part => [part.tool_call_id, part.status]))
      .toEqual([["a", "running"], ["b", "completed"]]);
    expect(parts.every(part => part.source.model_call_id === "model-1")).toBe(true);
    expect(responseItemsToTimelineItems(parts).map(item => item.kind === "aggregated_tool"
      && { id: item.id, active: item.active, failed: item.failed, declared: item.declared }))
      .toEqual([{ id: "a", active: true, failed: false, declared: false }, { id: "b", active: false, failed: false, declared: false }]);
    const refreshed = applySnapshotState(state, {
      snapshot_seq: sequence, stream_status: "open", agent_loop_status: "running", current_attempt: 1,
      blocks: [], tool_executions: state.toolExecutions,
      tool_calls: ["a", "b"].map(id => ({ ...state.toolCalls[id], tool_call_id: id, tool_name: "read" })), model_calls: [],
      activities: [], resource_refs: [], resumable: false,
    }, identity);
    const b = messageStreamToResponseItems(refreshed).find(part => part.tool_call_id === "b");
    expect(messageStreamToResponseItems(refreshed).filter(part => part.kind === "tool_call")
      .map(part => part.tool_call_id)).toEqual(["a", "b"]);
    expect(b?.tool_execution?.execution_completed_at).toBe("2026-11-23T00:00:04.500Z");
    expect(refreshed.toolExecutions.find(tool => tool.tool_call_id === "b")?.completed_at).toBe("2026-11-23T00:00:05Z");
    expect(parts.find(part => part.tool_call_id === "a")?.tool_execution?.execution_completed_at).toBeUndefined();
  });

  test("工具按真实生命周期排序，后续参数更新不修改原投影", () => {
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

    const parts = messageStreamToResponseItems(state);

    expect(parts.map((part) => part.id)).toEqual([
      "call_1",
      "call_1:result",
      "block_after_tool",
    ]);
    expect(parts[1]?.text).toBe("权威结果");

    const updated = applyMessageStreamEvent(state, {
      event_id: "evt_arguments", event_seq: 1, type: "tool_call",
      session_id: "ses_1", thread_id: "thr_1", turn_id: "turn_1", turn_stream_id: "strm_1",
      payload: { tool_call_id: "call_1", arguments: { path: "changed.md" } },
    });
    expect(messageStreamToResponseItems(updated)[0]?.arguments).toBe('{"path":"changed.md"}');
    expect(messageStreamToResponseItems(state)).toEqual(parts);
  });

  test("同一调用多个attempt明确拒绝，不按时序或ID挑选结果", () => {
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

    expect(() => messageStreamToResponseItems(state)).toThrow("同一工具调用存在多个attempt");
  });
});
