import { describe, expect, test } from "bun:test";
import {
  applyMessageStreamEvent,
  createMessageStreamState,
  messageStreamToResponseParts,
  type MessageStreamEvent,
} from "../messageStream";

function event(
  seq: number,
  type: MessageStreamEvent["type"],
  payload: Record<string, unknown>,
): MessageStreamEvent {
  return {
    event_id: `evt_${seq}`,
    session_id: "ses_1",
    turn_id: "turn_1",
    turn_stream_id: "strm_1",
    event_seq: seq,
    type,
    payload,
  };
}

describe("message stream reducer", () => {
  test("按 event_seq 聚合 reasoning/text，并对重复事件幂等", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "block.started", {
      block_id: "block_reasoning",
      block_index: 0,
      carrier_type: "reasoning",
    }));
    state = applyMessageStreamEvent(state, event(3, "block.delta", {
      block_id: "block_reasoning",
      carrier_type: "reasoning",
      operation: "append",
      text: "先",
    }));
    state = applyMessageStreamEvent(state, event(4, "block.delta", {
      block_id: "block_reasoning",
      carrier_type: "reasoning",
      operation: "append",
      text: "思考",
    }));
    state = applyMessageStreamEvent(state, event(5, "block.started", {
      block_id: "block_text",
      block_index: 1,
      carrier_type: "text",
    }));
    state = applyMessageStreamEvent(state, event(6, "block.delta", {
      block_id: "block_text",
      carrier_type: "text",
      operation: "append",
      text: "回答",
    }));
    const duplicated = applyMessageStreamEvent(state, event(6, "block.delta", {
      block_id: "block_text",
      carrier_type: "text",
      operation: "append",
      text: "不应重复",
    }));

    expect(duplicated.lastEventSeq).toBe(6);
    expect(duplicated.blocks.map((block) => block.text)).toEqual(["先思考", "回答"]);
    expect(messageStreamToResponseParts(duplicated).map((part) => part.text)).toEqual([
      "先思考",
      "回答",
    ]);
  });

  test("snapshot 是权威替换，缺口不会伪造连续状态", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    const gap = applyMessageStreamEvent(state, event(3, "block.delta", {
      block_id: "b1",
      operation: "append",
      text: "不能直接应用",
    }));
    expect(gap.connectionStatus).toBe("gap");
    expect(gap.blocks).toHaveLength(0);

    const recovered = applyMessageStreamEvent(gap, event(8, "stream.snapshot", {
      snapshot_seq: 8,
      stream_status: "interrupting",
      agent_loop_status: "tool_running",
      current_attempt: 2,
      blocks: [{
        block_id: "b1",
        block_index: 0,
        carrier_type: "reasoning",
        status: "completed",
        text: "已恢复",
        items: [],
      }],
      tool_calls: [{
        tool_call_id: "call_1",
        tool_name: "shell",
        arguments: { command: "pwd" },
        status: "streaming",
      }],
      tool_executions: [],
      interrupt_state: { request_id: "intr_1", status: "requested" },
      resumable: true,
    }));
    expect(recovered.lastEventSeq).toBe(8);
    expect(recovered.connectionStatus).toBe("connected");
    expect(recovered.streamStatus).toBe("interrupting");
    expect(recovered.interruptState?.requestId).toBe("intr_1");
    expect(recovered.blocks[0]?.text).toBe("已恢复");
    expect(recovered.toolCalls.call_1?.arguments).toEqual({ command: "pwd" });
  });

  test("缺口事件先缓冲，补齐高水位后按 event_seq 自动回放", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(3, "block.delta", {
      block_id: "b1",
      carrier_type: "text",
      operation: "append",
      text: "后半段",
    }));
    expect(state.lastEventSeq).toBe(1);
    expect(state.pendingEvents.map((item) => item.event_seq)).toEqual([3]);
    state = applyMessageStreamEvent(state, event(2, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
    }));
    expect(state.lastEventSeq).toBe(3);
    expect(state.pendingEvents).toHaveLength(0);
    expect(state.connectionStatus).toBe("connected");
    expect(state.blocks[0]?.text).toBe("后半段");
  });

  test("snapshot 只推进自己的高水位，不吞掉更晚的并发事件", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(4, "block.delta", {
      block_id: "b1",
      operation: "append",
      text: "并发事件",
    }));
    state = applyMessageStreamEvent(state, event(2, "stream.snapshot", {
      snapshot_seq: 2,
      stream_status: "open",
      agent_loop_status: "text",
      current_attempt: 1,
      blocks: [],
      tool_calls: [],
      tool_executions: [],
      resumable: true,
    }));
    expect(state.lastEventSeq).toBe(2);
    expect(state.pendingEvents.map((item) => item.event_seq)).toEqual([4]);
    expect(state.connectionStatus).toBe("gap");
  });

  test("旧 snapshot 不能覆盖已经收到的新 delta", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "block.started", {
      block_id: "block_1",
      block_index: 0,
      carrier_type: "text",
    }));
    state = applyMessageStreamEvent(state, event(3, "block.delta", {
      block_id: "block_1",
      block_index: 0,
      carrier_type: "text",
      operation: "append",
      text: "新内容",
    }));

    const stale = applyMessageStreamEvent(state, event(2, "stream.snapshot", {
      snapshot_seq: 2,
      stream_status: "open",
      agent_loop_status: "model_running",
      current_attempt: 1,
      blocks: [],
      tool_calls: [],
      tool_executions: [],
      resumable: true,
    }));

    expect(stale.lastEventSeq).toBe(3);
    expect(stale.blocks[0]?.text).toBe("新内容");
  });

  test("stream.failed 和 stream.interrupted 都是明确终态", () => {
    let failed = createMessageStreamState("ses_1", "turn_1");
    failed = applyMessageStreamEvent(failed, event(1, "stream.failed", {
      code: "execution_lost",
      message: "后端重启导致执行丢失",
      after_interrupt_requested: false,
      resumable: false,
    }));
    expect(failed.streamStatus).toBe("failed");
    expect(failed.connectionStatus).toBe("terminal");
    expect(failed.failure?.code).toBe("execution_lost");

    let interrupted = createMessageStreamState("ses_1", "turn_1");
    interrupted = applyMessageStreamEvent(interrupted, event(1, "stream.interrupted", {
      interrupt_request_id: "intr_1",
      status: "interrupted",
    }));
    expect(interrupted.streamStatus).toBe("interrupted");
    expect(interrupted.interruptState?.status).toBe("confirmed");

    let interruptedWithBlock = createMessageStreamState("ses_1", "turn_1");
    interruptedWithBlock = applyMessageStreamEvent(interruptedWithBlock, event(1, "block.started", {
      block_id: "block_running",
      block_index: 0,
      carrier_type: "reasoning",
    }));
    interruptedWithBlock = applyMessageStreamEvent(interruptedWithBlock, event(2, "block.delta", {
      block_id: "block_running",
      operation: "append",
      text: "半截思考",
    }));
    interruptedWithBlock = applyMessageStreamEvent(interruptedWithBlock, event(3, "stream.interrupted", {
      interrupt_request_id: "intr_1",
      status: "interrupted",
    }));
    expect(interruptedWithBlock.blocks[0]?.status).toBe("interrupted");
  });

  test("同一 Turn 不接受变化后的 turn_stream_id", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    const changed = applyMessageStreamEvent(state, {
      ...event(2, "block.delta", {
        block_id: "b1",
        operation: "append",
        text: "不能应用",
      }),
      turn_stream_id: "strm_other",
    });
    expect(changed.connectionStatus).toBe("gap");
    expect(changed.blocks).toHaveLength(0);
    expect(changed.protocolError).toContain("turn_stream_id");
  });

  test("工具结果未知时保留可展示的未知事实", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "tool_call", {
      tool_call_id: "call_1",
      tool_name: "shell",
      arguments: { command: "touch side-effect" },
    }));
    state = applyMessageStreamEvent(state, event(2, "tool.started", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "shell",
    }));
    state = applyMessageStreamEvent(state, event(3, "stream.failed", {
      code: "execution_lost",
      message: "后端重启",
      after_interrupt_requested: false,
      resumable: false,
    }));
    expect(state.toolExecutions[0]?.status).toBe("completed");
    expect(state.toolExecutions[0]?.outcome).toBe("outcome_unknown");
    const parts = messageStreamToResponseParts({
      ...state,
      toolExecutions: [{
        ...state.toolExecutions[0]!,
        status: "completed",
        outcome: "outcome_unknown",
      }],
    });
    const toolPart = parts.find((part) => part.kind === "tool_call");
    expect(toolPart?.outcome_unknown).toBe(true);
    expect(toolPart?.arguments).toBe('{"command":"touch side-effect"}');
  });

  test("工具调用分片不因空名称和空参数覆盖已有信息", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "tool_call", {
      tool_call_id: "call_1",
      tool_name: "invoke_custom_tool",
      arguments: { tool_name: "unknown_tool" },
    }));
    state = applyMessageStreamEvent(state, event(2, "tool_call", {
      tool_call_id: "call_1",
      tool_name: "",
      arguments: {},
    }));
    state = applyMessageStreamEvent(state, event(3, "tool.started", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "unknown_tool",
    }));

    const toolPart = messageStreamToResponseParts(state).find(
      (part) => part.kind === "tool_call",
    );
    expect(toolPart?.tool_name).toBe("unknown_tool");
    expect(toolPart?.arguments).toBe('{"tool_name":"unknown_tool"}');
  });

  test("model.retrying 不提前结束 stream，并允许下一 attempt 接续", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "model.started", {
      model_call_id: "model_1",
      attempt: 1,
    }));
    state = applyMessageStreamEvent(state, event(2, "model.completed", {
      model_call_id: "model_1",
      outcome: "validation_failed",
    }));
    state = applyMessageStreamEvent(state, event(3, "model.retrying", {
      model_call_id: "model_1",
      attempt: 1,
      reason: "需要补齐工具结果",
    }));
    state = applyMessageStreamEvent(state, event(4, "model.started", {
      model_call_id: "model_2",
      attempt: 2,
    }));
    expect(state.streamStatus).toBe("open");
    expect(state.agentLoopStatus).toBe("model_running");
    expect(state.currentModelCallId).toBe("model_2");
    expect(state.currentAttempt).toBe(2);
  });

  test("校验重试会隐藏上一 attempt 的中间文本", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, {
      ...event(1, "model.started", {
        model_call_id: "model_1",
        attempt: 1,
      }),
      model_call_id: "model_1",
    });
    state = applyMessageStreamEvent(state, {
      ...event(2, "block.started", {
        block_id: "answer_1",
        block_index: 0,
        carrier_type: "text",
      }),
      model_call_id: "model_1",
    });
    state = applyMessageStreamEvent(state, event(3, "block.delta", {
      block_id: "answer_1",
      operation: "append",
      text: "中间答案",
    }));
    state = applyMessageStreamEvent(state, event(4, "model.completed", {
      model_call_id: "model_1",
      outcome: "validation_failed",
    }));
    state = applyMessageStreamEvent(state, event(5, "model.retrying", {
      model_call_id: "model_1",
      attempt: 1,
      reason: "需要重试",
    }));

    expect(state.blocks[0]?.projection).toBe("intermediate");
    expect(messageStreamToResponseParts(state)).toEqual([]);
  });

  test("tool_call 未完成且没有执行结果时单独展示为失败调用", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "tool_call.delta", {
      tool_call_id: "call_incomplete",
      tool_name: "shell",
      arguments: { command: "pwd" },
      arguments_complete: false,
    }));
    state = applyMessageStreamEvent(state, event(2, "tool_call.completed", {
      tool_call_id: "call_incomplete",
      tool_name: "shell",
      status: "cancelled",
      completion_reason: "user_interrupt",
      arguments_complete: false,
    }));
    const part = messageStreamToResponseParts(state).find(
      (item) => item.tool_call_id === "call_incomplete",
    );
    expect(part?.status).toBe("cancelled");
    expect(part?.final).toBe(true);
    expect(part?.arguments).toBe('{"command":"pwd"}');
  });

  test("snapshot 恢复统一 active_state、Activity 和 execution_lost", () => {
    const state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(7, "stream.snapshot", {
        snapshot_seq: 7,
        stream_status: "failed",
        agent_loop_status: "failed",
        current_attempt: 1,
        blocks: [],
        tool_calls: [],
        tool_executions: [{
          tool_execution_id: "exec_1",
          tool_call_id: "call_1",
          tool_name: "browser",
          status: "completed",
          outcome: "outcome_unknown",
          completion_reason: "execution_lost",
        }],
        activities: [{
          activity_id: "activity_1",
          kind: "browser.session",
          scope_ref: "session",
          status: "unknown",
          detail_available: false,
          resource_refs: ["resource_1"],
        }],
        active_state: {
          kind: "activity",
          phase: "unknown",
          entity_id: "activity_1",
          status: "unknown",
        },
        resource_refs: [{ resource_id: "resource_1", status: "unknown" }],
        recovery: { mode: "execution_lost", resumable: false },
        resumable: false,
      }),
    );
    expect(state.activeState?.kind).toBe("activity");
    expect(state.activities[0]?.detail_available).toBe(false);
    expect(state.resourceRefs.resource_1?.status).toBe("unknown");
    expect(state.recovery?.mode).toBe("execution_lost");
    expect(state.toolExecutions[0]?.outcome).toBe("outcome_unknown");
  });

  test("partial block 不被投影成最终完成正文", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
    }));
    state = applyMessageStreamEvent(state, event(2, "block.delta", {
      block_id: "b1",
      operation: "append",
      text: "半截",
    }));
    state = applyMessageStreamEvent(state, event(3, "block.completed", {
      block_id: "b1",
      status: "completed",
      partial: true,
      completion_reason: "user_interrupt",
    }));
    expect(messageStreamToResponseParts(state)[0]?.final).toBe(false);
  });

  test("四个阶段的 snapshot hydration 都保留完整活动实体投影", () => {
    const cases = [
      {
        phase: "reasoning",
        activeState: { kind: "model_output", phase: "reasoning", entity_id: "b_reasoning", status: "running" },
        blocks: [{ block_id: "b_reasoning", block_index: 0, carrier_type: "reasoning", status: "running", text: "思考", items: [], partial: true }],
      },
      {
        phase: "text",
        activeState: { kind: "model_output", phase: "text", entity_id: "b_text", status: "running" },
        blocks: [{ block_id: "b_text", block_index: 0, carrier_type: "text", status: "running", text: "回答", items: [], partial: false }],
      },
      {
        phase: "tool_call",
        activeState: { kind: "tool_call", phase: "arguments", entity_id: "call_1", status: "running" },
        blocks: [],
        tool_calls: [{ tool_call_id: "call_1", tool_name: "shell", arguments: { command: "pwd" }, arguments_complete: false }],
      },
      {
        phase: "tool_execution",
        activeState: { kind: "tool_execution", phase: "running", entity_id: "exec_1", status: "running" },
        blocks: [],
        tool_executions: [{ tool_execution_id: "exec_1", tool_call_id: "call_1", tool_name: "shell", status: "running" }],
      },
    ] satisfies Array<Record<string, unknown>>;

    for (const [index, item] of cases.entries()) {
      const state = applyMessageStreamEvent(
        createMessageStreamState("ses_1", `turn_${index}`),
        {
          ...event(1, "stream.snapshot", {
            snapshot_seq: 1,
            stream_status: "open",
            agent_loop_status: item.phase,
            current_attempt: 1,
            blocks: item.blocks ?? [],
            tool_calls: item.tool_calls ?? [],
            tool_executions: item.tool_executions ?? [],
            active_state: item.activeState,
            resumable: true,
          }),
          turn_id: `turn_${index}`,
        },
      );
      expect(state.activeState?.phase).toBe(
        (item.activeState as { phase: string }).phase,
      );
      expect(state.blocks.length + Object.keys(state.toolCalls).length + state.toolExecutions.length).toBeGreaterThan(0);
    }
  });

  test("snapshot 按实体生命周期序号排序，不使用数组位置或 updated_at 推断顺序", () => {
    const state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(20, "stream.snapshot", {
        snapshot_seq: 20,
        stream_status: "open",
        agent_loop_status: "model_running",
        current_attempt: 2,
        blocks: [
          {
            block_id: "block_2",
            block_index: 0,
            carrier_type: "text",
            status: "completed",
            text: "第二个 ModelCall",
            items: [],
            started_seq: 14,
            last_event_seq: 16,
            completed_seq: 16,
            started_at: "2026-08-24T00:00:14Z",
            updated_at: "2026-08-24T00:00:01Z",
            completed_at: "2026-08-24T00:00:16Z",
          },
          {
            block_id: "block_1",
            block_index: 0,
            carrier_type: "text",
            status: "completed",
            text: "第一个 ModelCall",
            items: [],
            started_seq: 4,
            last_event_seq: 8,
            completed_seq: 8,
            started_at: "2026-08-24T00:00:04Z",
            updated_at: "2026-08-24T00:00:20Z",
            completed_at: "2026-08-24T00:00:08Z",
          },
        ],
        tool_calls: [
          {
            tool_call_id: "call_2",
            tool_name: "shell",
            arguments: { command: "second" },
            status: "incomplete",
            started_seq: 18,
          },
          {
            tool_call_id: "call_1",
            tool_name: "shell",
            arguments: { command: "first" },
            status: "incomplete",
            started_seq: 9,
          },
        ],
        tool_executions: [
          {
            tool_execution_id: "exec_2",
            tool_call_id: "call_2",
            tool_name: "shell",
            status: "completed",
            outcome: "success",
            started_seq: 19,
            last_event_seq: 20,
            completed_seq: 20,
          },
        ],
        model_calls: [
          {
            model_call_id: "model_2",
            attempt: 2,
            status: "running",
            started_seq: 13,
            last_event_seq: 14,
          },
          {
            model_call_id: "model_1",
            attempt: 1,
            status: "completed",
            started_seq: 2,
            last_event_seq: 8,
            completed_seq: 8,
          },
        ],
        activities: [
          {
            activity_id: "compaction_2",
            kind: "context.compaction",
            scope_ref: "turn",
            status: "running",
            started_seq: 12,
            last_event_seq: 12,
            updated_at: "2026-08-24T00:00:02Z",
            resource_refs: [],
          },
          {
            activity_id: "compaction_1",
            kind: "context.compaction",
            scope_ref: "turn",
            status: "completed",
            outcome: "success",
            started_seq: 7,
            last_event_seq: 11,
            completed_seq: 11,
            updated_at: "2026-08-24T00:00:19Z",
            resource_refs: [],
          },
        ],
        resumable: true,
      }),
    );

    expect(state.blocks.map((block) => block.block_id)).toEqual(["block_1", "block_2"]);
    expect(state.activities.map((activity) => activity.activity_id)).toEqual([
      "compaction_1",
      "compaction_2",
    ]);
    expect(Object.keys(state.modelCalls)).toEqual(["model_2", "model_1"]);
    const parts = messageStreamToResponseParts(state);
    expect(parts.map((part) => part.part_id)).toEqual([
      "block_1",
      "call_1",
      "block_2",
      "exec_2",
    ]);
    expect(parts[1]?.arguments).toBe('{"command":"first"}');
    expect(state.blocks[0]?.started_seq).toBe(4);
    expect(state.activities[1]?.updated_at).toBe("2026-08-24T00:00:02Z");
  });

  test("连续两次压缩在 snapshot 高水位后继续按 event_seq 回放", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(5, "stream.snapshot", {
      snapshot_seq: 5,
      stream_status: "open",
      agent_loop_status: "model_running",
      current_attempt: 2,
      model_calls: [
        {
          model_call_id: "model_1",
          status: "completed",
          started_seq: 2,
          last_event_seq: 3,
          completed_seq: 3,
        },
        {
          model_call_id: "model_2",
          status: "running",
          started_seq: 4,
          last_event_seq: 5,
        },
      ],
      activities: [
        {
          activity_id: "compaction_1",
          kind: "context.compaction",
          scope_ref: "turn",
          status: "completed",
          started_seq: 3,
          last_event_seq: 3,
          completed_seq: 3,
          resource_refs: [],
        },
        {
          activity_id: "compaction_2",
          kind: "context.compaction",
          scope_ref: "turn",
          status: "running",
          started_seq: 5,
          last_event_seq: 5,
          resource_refs: [],
        },
      ],
      blocks: [],
      tool_calls: [],
      tool_executions: [],
      resumable: true,
    }));
    state = applyMessageStreamEvent(state, event(6, "activity.completed", {
      activity_id: "compaction_2",
      kind: "context.compaction",
      status: "completed",
      outcome: "success",
    }));
    state = applyMessageStreamEvent(state, event(7, "model.started", {
      model_call_id: "model_3",
      attempt: 3,
    }));

    expect(state.lastEventSeq).toBe(7);
    expect(state.activities.map((activity) => activity.activity_id)).toEqual([
      "compaction_1",
      "compaction_2",
    ]);
    expect(state.activities[1]?.completed_seq).toBe(6);
    expect(state.modelCalls.model_3?.started_seq).toBe(7);
    expect(state.connectionStatus).toBe("connected");
  });
});
