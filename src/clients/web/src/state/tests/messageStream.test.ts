import { describe, expect, test } from "bun:test";
import {
  applyMessageStreamEvent,
  createMessageStreamState,
  messageStreamToResponseParts,
  type MessageStreamEvent,
  type MessageStreamState,
  type MessageStreamToolExecution,
  writeMessageStreamCache,
} from "../messageStream/index";
import { isTerminalStatus } from "../messageStream/state";
import { validateMessageStreamSnapshotPayload } from "../../api/messageStreamSnapshot";

function event(
  seq: number,
  type: MessageStreamEvent["type"],
  payload: Record<string, unknown>,
): MessageStreamEvent {
  const envelope = {
    event_id: `evt_${seq}`,
    session_id: "ses_1",
    turn_id: "turn_1",
    turn_stream_id: "strm_1",
    event_seq: seq,
  };
  if (type === "stream.snapshot") {
    return {
      ...envelope,
      type,
      payload: validateMessageStreamSnapshotPayload({
        blocks: [],
        tool_executions: [],
        tool_calls: [],
        model_calls: [],
        activities: [],
        resource_refs: [],
        ...payload,
      }),
    };
  }
  return { ...envelope, type, payload };
}

describe("message stream reducer", () => {
  test("乱序事件缓冲有明确上限，溢出后要求 snapshot 恢复", () => {
    let state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.opened", { status: "open" }),
    );
    for (let seq = 3; seq < 303; seq += 1) {
      state = applyMessageStreamEvent(state, event(seq, "block.delta", {
        block_id: "b1",
        operation: "append",
        text: "x",
      }));
    }

    expect(state.pendingEvents).toHaveLength(256);
    expect(state.pendingEvents[0]?.event_seq).toBe(3);
    expect(state.pendingEvents[255]?.event_seq).toBe(258);
    expect(state.protocolError).toContain("必须通过 snapshot 恢复");
  });

  test("全局流缓存仅保留最近八个终态 Turn，并保留活动流", () => {
    let streams = new Map<string, ReturnType<typeof createMessageStreamState>>();
    for (let index = 0; index < 12; index += 1) {
      const state = {
        ...createMessageStreamState("ses_1", `turn_${index}`, `strm_${index}`),
        streamStatus: "completed" as const,
        connectionStatus: "terminal" as const,
      };
      streams = writeMessageStreamCache(streams, state.turnStreamId, state);
    }
    const active = createMessageStreamState("ses_1", "turn_live", "strm_live");
    streams = writeMessageStreamCache(streams, active.turnStreamId, active);

    expect([...streams.keys()]).toEqual([
      "strm_4",
      "strm_5",
      "strm_6",
      "strm_7",
      "strm_8",
      "strm_9",
      "strm_10",
      "strm_11",
      "strm_live",
    ]);
  });

  test("超大 live 文本和工具结果保持有界并显示明确截断标记", () => {
    let state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.opened", { status: "open" }),
    );
    state = applyMessageStreamEvent(state, event(2, "block.started", {
      block_id: "block_large",
      block_index: 0,
      carrier_type: "text",
    }));
    state = applyMessageStreamEvent(state, event(3, "block.delta", {
      block_id: "block_large",
      operation: "append",
      text: "a".repeat(200_000),
    }));
    state = applyMessageStreamEvent(state, event(4, "block.delta", {
      block_id: "block_large",
      operation: "append",
      text: "b".repeat(200_000),
    }));
    state = applyMessageStreamEvent(state, event(5, "tool.started", {
      tool_execution_id: "tool_large",
      tool_call_id: "call_large",
      tool_name: "large_tool",
    }));
    state = applyMessageStreamEvent(state, event(6, "tool.completed", {
      tool_execution_id: "tool_large",
      tool_call_id: "call_large",
      tool_name: "large_tool",
      status: "completed",
      result: "r".repeat(100_000),
    }));

    expect(state.blocks[0]?.text).toHaveLength(256 * 1024);
    expect(state.blocks[0]?.text.startsWith("a")).toBe(true);
    expect(state.blocks[0]?.text.endsWith("b")).toBe(true);
    expect(state.blocks[0]?.text).toContain("消息流展示已截断");
    expect(state.toolExecutions[0]?.result).toHaveLength(64 * 1024);
    expect(state.toolExecutions[0]?.result).toContain("消息流展示已截断");
  });

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

  test("同一 tool_call 的 reconciled 收口 execution 不再渲染第二个工具", () => {
    // 复现后端竞态契约：请求边界 ToolMessage 先完成 canonical 结果，
    // 迟到的 on_tool_start/on_tool_end 再写一条 reconciled_tool_message
    // 收口事件。投影必须折叠成一个逻辑工具。
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "model.started", {
      model_call_id: "mc_1",
      attempt: 1,
    }));
    state = applyMessageStreamEvent(state, event(2, "tool_call.delta", {
      tool_call_id: "call_1",
      tool_name: "read_file",
      arguments: { path: "README.md" },
      status: "accumulating",
    }));
    state = applyMessageStreamEvent(state, event(3, "tool.completed", {
      tool_execution_id: "exec_canonical",
      tool_call_id: "call_1",
      tool_name: "read_file",
      status: "completed",
      outcome: "success",
      completion_reason: "tool_completed",
      result: "README 内容",
    }));
    state = applyMessageStreamEvent(state, event(4, "block.started", {
      block_id: "b1",
      carrier_type: "text",
      model_call_id: "mc_1",
    }));
    state = applyMessageStreamEvent(state, event(5, "block.delta", {
      block_id: "b1",
      carrier_type: "text",
      operation: "append",
      text: "已读取 README。",
    }));
    state = applyMessageStreamEvent(state, event(6, "tool.started", {
      tool_execution_id: "exec_late",
      tool_call_id: "call_1",
      tool_name: "read_file",
    }));
    state = applyMessageStreamEvent(state, event(7, "tool.completed", {
      tool_execution_id: "exec_late",
      tool_call_id: "call_1",
      tool_name: "read_file",
      status: "completed",
      outcome: "success",
      completion_reason: "reconciled_tool_message",
      result: "README 内容",
    }));
    const parts = messageStreamToResponseParts(state);
    const kinds = parts.map((part) => part.kind);
    expect(kinds.filter((kind) => kind === "tool_call")).toHaveLength(1);
    expect(kinds.filter((kind) => kind === "tool_result")).toHaveLength(1);
    expect(parts.find((part) => part.kind === "tool_call")?.tool_name).toBe("read_file");
    expect(parts.find((part) => part.kind === "tool_call")?.arguments).toBe(
      '{"path":"README.md"}',
    );
    // 折叠后保留 canonical execution 的时序位置：工具在最终正文之前。
    expect(kinds).toEqual(["tool_call", "tool_result", "text"]);
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

  test("工具终态 outcome 逐值透传，未识别取值不写入", () => {
    const cases: Array<{
      outcome: unknown;
      expected: MessageStreamToolExecution["outcome"];
      expectedStatus: "completed" | "failed";
    }> = [
      { outcome: "success", expected: "success", expectedStatus: "completed" },
      { outcome: "execution_lost", expected: "execution_lost", expectedStatus: "completed" },
      { outcome: "provider_error", expected: "provider_error", expectedStatus: "failed" },
      { outcome: "outcome_unknown", expected: "outcome_unknown", expectedStatus: "failed" },
      // 未识别取值必须落到 undefined，不得伪造出 outcome
      { outcome: "validation_failed", expected: undefined, expectedStatus: "completed" },
      { outcome: undefined, expected: undefined, expectedStatus: "completed" },
    ];
    for (const item of cases) {
      // tool.started 只带 running 状态，终态 outcome 必须由 tool.completed 透传进来
      let state = createMessageStreamState("ses_1", "turn_1");
      state = applyMessageStreamEvent(state, event(1, "tool.started", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
      }));
      state = applyMessageStreamEvent(state, event(2, "tool.completed", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
        status: "completed",
        outcome: item.outcome,
      }));
      expect(state.toolExecutions[0]?.status).toBe("completed");
      expect(state.toolExecutions[0]?.outcome).toBe(item.expected);

      const parts = messageStreamToResponseParts(state);
      const toolPart = parts.find((part) => part.kind === "tool_call");
      expect(toolPart?.status).toBe(item.expectedStatus);
      expect(toolPart?.outcome_unknown).toBe(item.expected === "outcome_unknown");
      expect(parts.find((part) => part.kind === "tool_result")?.status).toBe(item.expectedStatus);
    }
  });

  test("工具信封身份可补入 payload 并在 active_state 中保留", () => {
    let state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      {
        ...event(1, "stream.opened", { status: "open" }),
        workspace_id: "workspace_1",
      },
    );
    state = applyMessageStreamEvent(state, {
      ...event(2, "tool.started", { tool_name: "shell" }),
      workspace_id: "workspace_1",
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "invocation_1",
      tool_attempt_id: "attempt_1",
    });
    expect(state.workspaceId).toBe("workspace_1");
    expect(state.toolExecutions[0]).toMatchObject({
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "invocation_1",
      tool_attempt_id: "attempt_1",
    });
    expect(state.activeState).toMatchObject({
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "invocation_1",
      tool_attempt_id: "attempt_1",
    });

    state = applyMessageStreamEvent(state, {
      ...event(3, "tool.completed", { tool_name: "shell", status: "completed" }),
      workspace_id: "workspace_1",
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "invocation_1",
      tool_attempt_id: "attempt_1",
    });
    expect(state.toolExecutions[0]).toMatchObject({
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "invocation_1",
      tool_attempt_id: "attempt_1",
    });
    expect(state.activeState).toMatchObject({
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "invocation_1",
      tool_attempt_id: "attempt_1",
    });
  });

  test("模型和 block 的信封身份会补入前端实体", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, {
      ...event(1, "model.started", { attempt: 1 }),
      model_call_id: "model_1",
    });
    state = applyMessageStreamEvent(state, {
      ...event(2, "block.started", { block_index: 0, carrier_type: "text" }),
      model_call_id: "model_1",
      block_id: "block_1",
    });
    expect(state.modelCalls.model_1).toMatchObject({
      model_call_id: "model_1",
      status: "running",
    });
    expect(state.blocks[0]).toMatchObject({
      block_id: "block_1",
      model_call_id: "model_1",
    });
  });

  test("工具调用分片不因空名称和空参数覆盖已有信息", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "tool_call", {
      tool_call_id: "call_1",
      tool_name: "invoke_extension_tool",
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
    const parts = messageStreamToResponseParts(state);
    expect(parts[0]?.final).toBe(false);
    expect(parts[0]?.partial).toBe(true);
    expect(parts[0]?.completion_reason).toBe("user_interrupt");
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
      "exec_2:result",
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

  test("非法 activity status 收敛为 unknown，不冒充分已完成", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "activity.started", {
      activity_id: "compaction_1",
      kind: "context.compaction",
      scope_ref: "turn",
      status: "paused",
    }));

    expect(state.activities).toHaveLength(1);
    expect(state.activities[0]?.status).toBe("unknown");
  });

  test("snapshot 路径的非法 activity status 同样收敛为 unknown，不原样透传", () => {
    const state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "running",
        current_attempt: 1,
        activities: [{
          activity_id: "compaction_1",
          kind: "context.compaction",
          scope_ref: "turn",
          status: "paused",
          resource_refs: [],
        }],
        resumable: true,
      }),
    );

    expect(state.activities).toHaveLength(1);
    expect(state.activities[0]?.status).toBe("unknown");
    expect(state.activities[0]?.status).not.toBe("paused");
    expect(state.activities[0]?.status).not.toBe("completed");
  });

  test("snapshot 路径的合法 activity status 原样保留", () => {
    const statuses = ["running", "waiting", "stopping", "completed", "failed", "unknown"] as const;
    for (const status of statuses) {
      const state = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.snapshot", {
          snapshot_seq: 1,
          stream_status: "open",
          agent_loop_status: "running",
          current_attempt: 1,
          activities: [{
            activity_id: `compaction_${status}`,
            kind: "context.compaction",
            scope_ref: "turn",
            status,
            resource_refs: [],
          }],
          resumable: true,
        }),
      );
      expect(state.activities[0]?.status).toBe(status);
    }
  });

  test("snapshot 路径的非法 tool status 与事件路径收敛一致，不原样透传", () => {
    const snapshotState = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "tool_running",
        current_attempt: 1,
        tool_executions: [{
          tool_execution_id: "exec_1",
          tool_call_id: "call_1",
          tool_name: "shell",
          status: "paused",
        }],
        resumable: true,
      }),
    );

    let eventState = createMessageStreamState("ses_1", "turn_1");
    eventState = applyMessageStreamEvent(eventState, event(1, "tool.started", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "shell",
    }));
    eventState = applyMessageStreamEvent(eventState, event(2, "tool.completed", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "shell",
      status: "paused",
    }));

    expect(snapshotState.toolExecutions).toHaveLength(1);
    expect(snapshotState.toolExecutions[0]?.status).toBe(eventState.toolExecutions[0]?.status);
    expect(snapshotState.toolExecutions[0]?.status).toBe("running");
    expect(snapshotState.toolExecutions[0]?.status).not.toBe("paused");
    expect(snapshotState.toolExecutions[0]?.status).not.toBe("completed");
  });

  test("snapshot 路径与事件路径对 tool status 的收敛结果逐字一致", () => {
    const cases = [
      { status: "running", expected: "running", terminal: false },
      { status: "completed", expected: "completed", terminal: true },
      { status: "failed", expected: "failed", terminal: true },
      { status: "succeeded", expected: "completed", terminal: true },
      { status: "outcome_unknown", expected: "completed", terminal: true },
    ] as const;

    for (const [index, item] of cases.entries()) {
      const snapshotState = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.snapshot", {
          snapshot_seq: 1,
          stream_status: "open",
          agent_loop_status: "tool_running",
          current_attempt: 1,
          tool_executions: [{
            tool_execution_id: "exec_1",
            tool_call_id: "call_1",
            tool_name: "shell",
            status: item.status,
          }],
          resumable: true,
        }),
      );
      let eventState = createMessageStreamState("ses_1", "turn_1");
      eventState = applyMessageStreamEvent(eventState, event(1, "tool.started", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
      }));
      eventState = applyMessageStreamEvent(eventState, event(2, "tool.completed", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
        status: item.status,
      }));

      expect(snapshotState.toolExecutions[0]?.status)
        .toBe(eventState.toolExecutions[0]?.status);
      expect(snapshotState.toolExecutions[0]?.status).toBe(item.expected);
      if (item.terminal) {
        expect(eventState.toolExecutions[0]?.completed_seq).toBe(2);
      }
    }
  });

  test("snapshot 路径的非法 tool outcome 与事件路径收敛一致，不原样透传", () => {
    const snapshotState = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "tool_running",
        current_attempt: 1,
        tool_executions: [{
          tool_execution_id: "exec_1",
          tool_call_id: "call_1",
          tool_name: "shell",
          status: "completed",
          outcome: "weird",
        }],
        resumable: true,
      }),
    );

    let eventState = createMessageStreamState("ses_1", "turn_1");
    eventState = applyMessageStreamEvent(eventState, event(1, "tool.started", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "shell",
    }));
    eventState = applyMessageStreamEvent(eventState, event(2, "tool.completed", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "shell",
      status: "completed",
      outcome: "weird",
    }));

    expect(snapshotState.toolExecutions[0]?.outcome)
      .toBe(eventState.toolExecutions[0]?.outcome);
    expect(snapshotState.toolExecutions[0]?.outcome).toBeUndefined();
    expect(snapshotState.toolExecutions[0]?.outcome).not.toBe("weird");
  });

  test("snapshot 路径与事件路径对 tool outcome 的归一结果逐字一致", () => {
    const cases: Array<{ outcome: unknown; expected: MessageStreamToolExecution["outcome"] }> = [
      { outcome: "success", expected: "success" },
      { outcome: "provider_error", expected: "provider_error" },
      { outcome: "execution_lost", expected: "execution_lost" },
      { outcome: "outcome_unknown", expected: "outcome_unknown" },
      { outcome: "validation_failed", expected: undefined },
      { outcome: undefined, expected: undefined },
    ];

    for (const item of cases) {
      const snapshotState = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.snapshot", {
          snapshot_seq: 1,
          stream_status: "open",
          agent_loop_status: "tool_running",
          current_attempt: 1,
          tool_executions: [{
            tool_execution_id: "exec_1",
            tool_call_id: "call_1",
            tool_name: "shell",
            status: "completed",
            outcome: item.outcome,
          }],
          resumable: true,
        }),
      );
      let eventState = createMessageStreamState("ses_1", "turn_1");
      eventState = applyMessageStreamEvent(eventState, event(1, "tool.started", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
      }));
      eventState = applyMessageStreamEvent(eventState, event(2, "tool.completed", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
        status: "completed",
        outcome: item.outcome,
      }));

      expect(snapshotState.toolExecutions[0]?.outcome)
        .toBe(eventState.toolExecutions[0]?.outcome);
      expect(snapshotState.toolExecutions[0]?.outcome).toBe(item.expected);
    }
  });

  const ILLEGAL_TEXT_INPUTS: Array<{ label: string; value: unknown }> = [
    { label: "null", value: null },
    { label: "number", value: 42 },
    { label: "empty string", value: "" },
    { label: "object", value: { a: 1 } },
    { label: "boolean", value: true },
    { label: "array", value: ["x"] },
  ];

  function snapshotToolState(toolName: unknown, completionReason: unknown) {
    return applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "tool_running",
        current_attempt: 1,
        tool_executions: [{
          tool_execution_id: "exec_1",
          tool_call_id: "call_1",
          tool_name: toolName,
          status: "completed",
          completion_reason: completionReason,
        }],
        resumable: true,
      }),
    );
  }

  function eventToolState(toolName: unknown, completionReason: unknown) {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "tool.started", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: toolName,
    }));
    return applyMessageStreamEvent(state, event(2, "tool.completed", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: toolName,
      status: "completed",
      completion_reason: completionReason,
    }));
  }

  test("snapshot 路径的非法 tool_name 与 completion_reason 收敛到事件路径同一结果", () => {
    for (const item of ILLEGAL_TEXT_INPUTS) {
      const snapshotState = snapshotToolState(item.value, item.value);
      const eventState = eventToolState(item.value, item.value);
      const snapshotTool = snapshotState.toolExecutions[0];
      const eventTool = eventState.toolExecutions[0];

      expect(snapshotTool?.tool_name).toBe(eventTool?.tool_name);
      expect(snapshotTool?.completion_reason).toBe(eventTool?.completion_reason);
      // 非字符串输入不得原样透传
      expect(snapshotTool?.tool_name).not.toEqual(item.value);
      expect(snapshotTool?.completion_reason).not.toEqual(item.value);
      // 既有语义：tool_name 兜底为 "tool"，completion_reason 收敛为 undefined
      expect(snapshotTool?.tool_name).toBe("tool");
      expect(snapshotTool?.completion_reason).toBeUndefined();
    }
  });

  test("snapshot 路径与事件路径对 tool_name / completion_reason 的合法取值逐字一致", () => {
    const names = ["shell", "read_file", "a".repeat(200)];
    const reasons = ["tool_completed", "reconciled_tool_message", "execution_lost"];

    for (const name of names) {
      for (const reason of reasons) {
        const snapshotTool = snapshotToolState(name, reason).toolExecutions[0];
        const eventTool = eventToolState(name, reason).toolExecutions[0];
        expect(snapshotTool?.tool_name).toBe(eventTool?.tool_name);
        expect(snapshotTool?.tool_name).toBe(name);
        expect(snapshotTool?.completion_reason).toBe(eventTool?.completion_reason);
        expect(snapshotTool?.completion_reason).toBe(reason);
      }
    }
  });

  function snapshotActivityState(fields: Record<string, unknown>) {
    return applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "running",
        current_attempt: 1,
        activities: [{
          activity_id: "act_1",
          kind: "browser.session",
          status: "completed",
          resource_refs: [],
          ...fields,
        }],
        resumable: true,
      }),
    ).activities[0];
  }

  function eventActivityState(fields: Record<string, unknown>) {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "activity.started", {
      activity_id: "act_1",
      kind: "browser.session",
      status: "running",
    }));
    state = applyMessageStreamEvent(state, event(2, "activity.completed", {
      activity_id: "act_1",
      kind: "browser.session",
      status: "completed",
      ...fields,
    }));
    return state.activities[0];
  }

  test("snapshot 路径的非法 activity 文本字段收敛到事件路径同一结果", () => {
    const textFields = ["outcome", "summary", "detail_ref", "detail_error", "parent_activity_id"];
    for (const item of ILLEGAL_TEXT_INPUTS) {
      const fields = Object.fromEntries(textFields.map((field) => [field, item.value]));
      const snapshotActivity = snapshotActivityState(fields);
      const eventActivity = eventActivityState(fields);
      for (const field of textFields) {
        expect(snapshotActivity?.[field as "outcome"]).toBe(eventActivity?.[field as "outcome"]);
        expect(snapshotActivity?.[field as "outcome"]).toBeUndefined();
      }
      // 带兜底文案的字段：非字符串输入收敛为默认值
      expect(snapshotActivityState({ scope_ref: item.value })?.scope_ref).toBe("turn");
      expect(eventActivityState({ scope_ref: item.value })?.scope_ref).toBe("turn");
      expect(snapshotActivityState({ side_effect_policy: item.value })?.side_effect_policy).toBe("unknown");
      expect(eventActivityState({ side_effect_policy: item.value })?.side_effect_policy).toBe("unknown");
    }
  });

  test("snapshot 路径与事件路径对 activity 文本字段的合法取值逐字一致", () => {
    const cases: Array<Record<string, unknown>> = [
      { outcome: "success", summary: "抓取完成" },
      { outcome: "user_interrupt", summary: "" },
      { outcome: "provider_error", detail_ref: "detail_1", detail_error: "加载失败" },
      { scope_ref: "session", side_effect_policy: "read_only", parent_activity_id: "act_parent" },
    ];

    for (const fields of cases) {
      const snapshotActivity = snapshotActivityState(fields);
      const eventActivity = eventActivityState(fields);
      for (const field of Object.keys(fields)) {
        expect(snapshotActivity?.[field as "outcome"]).toBe(eventActivity?.[field as "outcome"]);
      }
    }
  });

  function snapshotBlockState(completionReason: unknown) {
    return applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "model_running",
        current_attempt: 1,
        blocks: [{
          block_id: "b1",
          items: [],
          status: "completed",
          completion_reason: completionReason,
        }],
        resumable: true,
      }),
    ).blocks[0];
  }

  function eventBlockState(completionReason: unknown) {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "block.started", { block_id: "b1" }));
    state = applyMessageStreamEvent(state, event(2, "block.completed", {
      block_id: "b1",
      status: "completed",
      completion_reason: completionReason,
    }));
    return state.blocks[0];
  }

  test("snapshot 路径的非法 block completion_reason 与事件路径收敛一致", () => {
    for (const item of ILLEGAL_TEXT_INPUTS) {
      const snapshotBlock = snapshotBlockState(item.value);
      const eventBlock = eventBlockState(item.value);
      expect(snapshotBlock?.completion_reason).toBe(eventBlock?.completion_reason);
      expect(snapshotBlock?.completion_reason).not.toEqual(item.value);
      expect(snapshotBlock?.completion_reason).toBe("upstream_completed");
    }
  });

  test("snapshot 路径与事件路径对 block completion_reason 的合法取值逐字一致", () => {
    for (const reason of ["upstream_completed", "user_interrupt", "carrier_switched"]) {
      const snapshotBlock = snapshotBlockState(reason);
      const eventBlock = eventBlockState(reason);
      expect(snapshotBlock?.completion_reason).toBe(eventBlock?.completion_reason);
      expect(snapshotBlock?.completion_reason).toBe(reason);
    }
  });

  test("运行中 block 的快照不发明 completion_reason，与 block.started 事件逐字段一致", () => {
    const snapshotState = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "running",
        current_attempt: 1,
        blocks: [{
          block_id: "b1",
          block_index: 0,
          items: [],
          status: "running",
          carrier_type: "text",
          projection: "streaming",
          text: "半截输出",
        }],
        resumable: true,
      }),
    );
    let eventState = createMessageStreamState("ses_1", "turn_1");
    eventState = applyMessageStreamEvent(eventState, event(1, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
      projection: "streaming",
    }));

    const snapshotBlock = snapshotState.blocks[0];
    const eventBlock = eventState.blocks[0];
    expect(snapshotBlock?.status).toBe("running");
    expect(snapshotBlock?.completion_reason).toBeUndefined();
    expect(eventBlock?.completion_reason).toBeUndefined();
    for (const field of ["status", "completion_reason", "partial", "carrier_type", "projection"] as const) {
      expect(snapshotBlock?.[field]).toBe(eventBlock?.[field]);
    }
  });

  test("运行中 block 的 projection 非 streaming 时仍按 status 判定，不发明 completion_reason", () => {
    // model.retrying 会把运行中 block 置为 projection="intermediate"，此时快照仍不得产出 completion_reason。
    const snapshotState = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "retrying",
        current_model_call_id: "mc_1",
        current_attempt: 2,
        blocks: [{
          block_id: "b1",
          block_index: 0,
          items: [],
          status: "running",
          carrier_type: "text",
          projection: "intermediate",
          text: "重试中的半截输出",
        }],
        resumable: true,
      }),
    );
    let eventState = createMessageStreamState("ses_1", "turn_1");
    eventState = applyMessageStreamEvent(eventState, event(1, "model.started", {
      model_call_id: "mc_1",
      attempt: 1,
    }));
    eventState = applyMessageStreamEvent(eventState, event(2, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
      projection: "streaming",
      model_call_id: "mc_1",
    }));
    eventState = applyMessageStreamEvent(eventState, event(3, "model.retrying", {
      model_call_id: "mc_1",
    }));

    const snapshotBlock = snapshotState.blocks[0];
    const eventBlock = eventState.blocks[0];
    expect(snapshotBlock?.projection).toBe("intermediate");
    expect(eventBlock?.projection).toBe("intermediate");
    expect(snapshotBlock?.completion_reason).toBeUndefined();
    expect(eventBlock?.completion_reason).toBeUndefined();
    for (const field of ["status", "completion_reason", "partial", "carrier_type", "projection"] as const) {
      expect(snapshotBlock?.[field]).toBe(eventBlock?.[field]);
    }
  });

  test("终态快照显式给出 completion_reason 时原样保留，不被兜底覆盖", () => {
    const block = snapshotBlockState("user_interrupt");
    expect(block?.status).toBe("completed");
    expect(block?.completion_reason).toBe("user_interrupt");
  });

  test("终态快照缺失 completion_reason 时兜底为 upstream_completed，与事件路径同一结果", () => {
    const missing = snapshotBlockState(undefined);
    expect(missing?.completion_reason).toBe("upstream_completed");
    expect(missing?.completion_reason).toBe(eventBlockState(undefined)?.completion_reason);
    for (const status of ["completed", "interrupted", "failed"] as const) {
      const state = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.snapshot", {
          snapshot_seq: 1,
          stream_status: "open",
          agent_loop_status: "running",
          current_attempt: 1,
          blocks: [{ block_id: "b1", items: [], status }],
          resumable: true,
        }),
      );
      expect(state.blocks[0]?.status).toBe(status);
      expect(state.blocks[0]?.completion_reason).toBe("upstream_completed");
    }
  });

  test("stream.completed 自动收口的 block 在事件路径与快照路径逐字段一致", () => {
    // 后端在 provider delta 晚于 model.completed 到达时，会在 stream.completed
    // 之前补发规范 block.completed(completion_reason=stream_completed, partial=false)。
    // 事件路径必须消费这些事件并落到与权威快照完全一致的 block 终态，
    // 否则 completion_reason/partial/final 会在两条链路间分叉。
    const blockA = {
      block_id: "b_auto_1",
      block_index: 0,
      items: [],
      status: "completed",
      carrier_type: "text",
      projection: "streaming",
      text: "已收口文本",
      completion_reason: "stream_completed",
      partial: false,
    };
    const blockB = {
      block_id: "b_auto_2",
      block_index: 1,
      items: [],
      status: "completed",
      carrier_type: "text",
      projection: "streaming",
      text: "迟到的最终文本",
      completion_reason: "stream_completed",
      partial: false,
    };
    const snapshotState = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 8,
        stream_status: "completed",
        agent_loop_status: "completed",
        current_attempt: 1,
        blocks: [blockA, blockB],
        resumable: false,
      }),
    );

    let eventState = createMessageStreamState("ses_1", "turn_1");
    eventState = applyMessageStreamEvent(eventState, event(1, "stream.opened", { status: "open" }));
    eventState = applyMessageStreamEvent(eventState, event(2, "model.started", {
      model_call_id: "mc_1",
      attempt: 1,
    }));
    eventState = applyMessageStreamEvent(eventState, event(3, "block.started", {
      block_id: "b_auto_1",
      block_index: 0,
      carrier_type: "text",
      projection: "streaming",
    }));
    eventState = applyMessageStreamEvent(eventState, event(4, "block.delta", {
      block_id: "b_auto_1",
      operation: "append",
      text: "已收口文本",
    }));
    eventState = applyMessageStreamEvent(eventState, event(5, "model.completed", {
      model_call_id: "mc_1",
      attempt: 1,
      outcome: "accepted",
    }));
    eventState = applyMessageStreamEvent(eventState, event(6, "block.started", {
      block_id: "b_auto_2",
      block_index: 1,
      carrier_type: "text",
      projection: "streaming",
    }));
    eventState = applyMessageStreamEvent(eventState, event(7, "block.delta", {
      block_id: "b_auto_2",
      operation: "append",
      text: "迟到的最终文本",
    }));
    eventState = applyMessageStreamEvent(eventState, event(8, "block.completed", {
      block_id: "b_auto_1",
      block_index: 0,
      carrier_type: "text",
      status: "completed",
      completion_reason: "stream_completed",
      partial: false,
    }));
    eventState = applyMessageStreamEvent(eventState, event(9, "block.completed", {
      block_id: "b_auto_2",
      block_index: 1,
      carrier_type: "text",
      status: "completed",
      completion_reason: "stream_completed",
      partial: false,
    }));
    eventState = applyMessageStreamEvent(eventState, event(10, "stream.completed", {
      status: "completed",
    }));

    const fields = ["status", "completion_reason", "partial", "carrier_type", "projection", "text"] as const;
    for (const eventBlock of eventState.blocks) {
      const snapshotBlock = snapshotState.blocks.find(
        (candidate) => candidate.block_id === eventBlock.block_id,
      );
      expect(snapshotBlock).toBeDefined();
      for (const field of fields) {
        expect(eventBlock[field]).toBe(snapshotBlock?.[field]);
      }
      expect(eventBlock.status).toBe("completed");
      expect(eventBlock.completion_reason).toBe("stream_completed");
      expect(eventBlock.partial).toBe(false);
    }
    expect(messageStreamToResponseParts(eventState)).toEqual(
      messageStreamToResponseParts(snapshotState),
    );
  });

  test("裸 stream.completed 不闭合仍 running 的 block，收口事实只能来自后端事件", () => {
    // 公共事件流在终态前会补发规范 block.completed；前端不得在 stream.completed
    // 分支无条件闭合 running block，否则会在 provider 仍可能补 delta 的真实场景
    // 下伪造错误终态（completion_reason 由后端权威决定，不能由前端发明）。
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "block.started", {
      block_id: "b_open",
      block_index: 0,
      carrier_type: "text",
      projection: "streaming",
    }));
    state = applyMessageStreamEvent(state, event(3, "stream.completed", { status: "completed" }));

    expect(state.streamStatus).toBe("completed");
    expect(state.blocks[0]?.status).toBe("running");
    expect(state.blocks[0]?.completion_reason).toBeUndefined();
  });

  test("snapshot 与事件路径对 block carrier_type / projection 的文本归一逐字一致", () => {
    for (const item of ILLEGAL_TEXT_INPUTS) {
      const snapshotState = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.snapshot", {
          snapshot_seq: 1,
          stream_status: "open",
          agent_loop_status: "model_running",
          current_attempt: 1,
          blocks: [{
            block_id: "b1",
            items: [],
            status: "running",
            carrier_type: item.value,
            projection: item.value,
          }],
          resumable: true,
        }),
      );
      let eventState = createMessageStreamState("ses_1", "turn_1");
      eventState = applyMessageStreamEvent(eventState, event(1, "block.started", {
        block_id: "b1",
        block_index: 0,
        carrier_type: item.value,
        projection: item.value,
      }));
      const snapshotBlock = snapshotState.blocks[0];
      const eventBlock = eventState.blocks[0];
      expect(snapshotBlock?.carrier_type).toBe(eventBlock?.carrier_type);
      expect(snapshotBlock?.carrier_type).toBe("text");
      expect(snapshotBlock?.projection).toBe(eventBlock?.projection);
      expect(snapshotBlock?.projection).toBe("streaming");
    }
  });

  test("snapshot 与事件路径对 interrupt reason 的非法文本归一逐字一致", () => {
    for (const item of ILLEGAL_TEXT_INPUTS) {
      const snapshotState = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.snapshot", {
          snapshot_seq: 1,
          stream_status: "interrupting",
          agent_loop_status: "running",
          current_attempt: 1,
          interrupt_state: { request_id: "intr_1", status: "requested", reason: item.value },
          resumable: true,
        }),
      );
      const eventState = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "interrupt.requested", { interrupt_request_id: "intr_1", reason: item.value }),
      );
      expect(snapshotState.interruptState?.reason)
        .toBe(eventState.interruptState?.reason);
      expect(snapshotState.interruptState?.reason).toBeUndefined();
    }
  });

  test("snapshot 与事件路径对空 failure.message 的结果逐字段一致", () => {
    // 后端 proto3 StreamFailure.message 无 presence：空串会在 SSE stream.snapshot
    // 控制帧里被省略（已用真实后端 codec 复现）。快照侧若无条件构造会伪造
    // message=undefined 的假 failure，而事件路径判定无效并返回 null。
    // 注意 HTML 快照 DTO 以 message 非空为契约会直接 500，但 SSE 控制帧不经 DTO，
    // 因此缺键形态是真实可达的。
    const wireFailureInputs: Array<Record<string, unknown>> = [
      { code: "execution_error" }, // SSE 控制帧：空 message 被 proto3 省略
      { code: "execution_error", message: "" },
      { code: "execution_error", message: null },
      { code: "execution_error", message: 42 },
      { code: "execution_error", message: { a: 1 } },
      { code: "execution_error", message: "真实失败原因" },
    ];
    for (const failure of wireFailureInputs) {
      const value = failure.message;
      const snapshotState = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.snapshot", {
          snapshot_seq: 1,
          stream_status: "failed",
          agent_loop_status: "failed",
          current_attempt: 1,
          failure,
          resumable: false,
        }),
      );
      const eventState = applyMessageStreamEvent(
        createMessageStreamState("ses_1", "turn_1"),
        event(1, "stream.failed", {
          code: "execution_error",
          message: value,
          after_interrupt_requested: false,
          resumable: false,
        }),
      );
      expect(snapshotState.failure).toEqual(eventState.failure);
      if (typeof value === "string" && value.length > 0) {
        expect(snapshotState.failure).toEqual({
          code: "execution_error",
          message: value,
          afterInterruptRequested: false,
          resumable: false,
        });
      } else {
        // message 不是非空字符串时，两条路径都不得伪造 failure
        expect(snapshotState.failure).toBeNull();
      }
    }
  });
});

describe("activity completion_reason 与 tool 展示状态", () => {
  test("Activity 终态收口不写公共协议无法表达的 completion_reason", () => {
    let interrupted = createMessageStreamState("ses_1", "turn_1");
    interrupted = applyMessageStreamEvent(interrupted, event(1, "activity.started", {
      activity_id: "act_1",
      kind: "browser.session",
      status: "running",
      side_effect_policy: "read_only",
    }));
    interrupted = applyMessageStreamEvent(interrupted, event(2, "stream.interrupted", {
      interrupt_request_id: "intr_1",
      status: "interrupted",
    }));
    // 终态收敛本身必须保留：只读 Activity 被用户中断即视为已完成。
    expect(interrupted.activities[0]?.status).toBe("completed");
    expect(interrupted.activities[0]?.outcome).toBe("user_interrupt");
    // completion_reason 未在公共 message.v1 的 Activity 中声明，codec 在
    // activity.* 事件投影与 snapshot activities 投影两处一律摘除，事件侧
    // 写入只会造出快照永远无法表达的不可恢复字段。
    expect(interrupted.activities[0]?.completion_reason).toBeUndefined();
    expect("completion_reason" in (interrupted.activities[0] ?? {})).toBe(false);

    let lost = createMessageStreamState("ses_1", "turn_1");
    lost = applyMessageStreamEvent(lost, event(1, "activity.started", {
      activity_id: "act_2",
      kind: "browser.session",
      status: "running",
      side_effect_policy: "read_only",
    }));
    lost = applyMessageStreamEvent(lost, event(2, "stream.failed", {
      code: "execution_lost",
      message: "后端重启",
      after_interrupt_requested: false,
      resumable: false,
    }));
    expect(lost.activities[0]?.status).toBe("failed");
    expect(lost.activities[0]?.completion_reason).toBeUndefined();
    expect("completion_reason" in (lost.activities[0] ?? {})).toBe(false);
  });

  test("事件侧不消费 payload 中被 codec 摘除的 activity completion_reason", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "activity.started", {
      activity_id: "act_1",
      kind: "browser.session",
      status: "running",
    }));
    state = applyMessageStreamEvent(state, event(2, "activity.completed", {
      activity_id: "act_1",
      kind: "browser.session",
      status: "completed",
      // codec 永远不会把该字段交给前端；事件侧不得保留它。
      completion_reason: "user_interrupt",
    }));
    expect(state.activities[0]?.status).toBe("completed");
    expect(state.activities[0]?.completion_reason).toBeUndefined();
    // snapshot 路径同样无法表达该字段，两条链路结果一致。
    const snapshotState = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", {
        snapshot_seq: 1,
        stream_status: "open",
        agent_loop_status: "activity_running",
        current_attempt: 1,
        activities: [{
          activity_id: "act_1",
          kind: "browser.session",
          status: "completed",
          resource_refs: [],
        }],
        resumable: true,
      }),
    );
    expect(snapshotState.activities[0]?.completion_reason)
      .toBe(state.activities[0]?.completion_reason);
  });

  test("工具展示状态有意与 state 层语义分叉，两者不得趋同", () => {
    const cases = [
      { outcome: "success", displayStatus: "completed" },
      { outcome: "outcome_unknown", displayStatus: "failed" },
      { outcome: "provider_error", displayStatus: "failed" },
    ] as const;
    for (const item of cases) {
      let state = createMessageStreamState("ses_1", "turn_1");
      state = applyMessageStreamEvent(state, event(1, "tool.started", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
      }));
      state = applyMessageStreamEvent(state, event(2, "tool.completed", {
        tool_execution_id: "exec_1",
        tool_call_id: "call_1",
        tool_name: "shell",
        status: "completed",
        outcome: item.outcome,
      }));
      // state 层必须与后端 tool.completed 的归一结果一致：completed。
      expect(state.toolExecutions[0]?.status).toBe("completed");
      // 展示层才是"结果未知/提供方出错不能当作成功"的降级语义。
      const toolPart = messageStreamToResponseParts(state)
        .find((part) => part.kind === "tool_call");
      expect(toolPart?.status).toBe(item.displayStatus);
    }
  });
});

// 对齐审计 P0-3：active_state 的 kind/phase 在前端事件路径与后端（及快照路径）取值不同。
// 期望值全部来自后端 message_stream_store.py 的真实分支（已用 codec 差分复现，0 mismatch）。
describe("active state kind 对齐", () => {
  const FIELDS = [
    "kind",
    "phase",
    "entity_id",
    "status",
    "last_kind",
    "last_phase",
    "reason",
    "tool_call_id",
    "tool_invocation_id",
    "tool_attempt_id",
    "tool_execution_id",
  ] as const;

  function activeStateOf(state: ReturnType<typeof createMessageStreamState>) {
    return state.activeState;
  }

  function snapshotOf(seq: number, activeState: Record<string, unknown>) {
    return applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(seq, "stream.snapshot", {
        snapshot_seq: seq,
        stream_status: "open",
        agent_loop_status: "running",
        current_attempt: 1,
        active_state: activeState,
        resumable: true,
      }),
    );
  }

  test("中断进行中：事件路径与快照路径都是 interrupting/stopping，并保留上一状态", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "model.started", { model_call_id: "mc_1", attempt: 1 }));
    state = applyMessageStreamEvent(state, event(3, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
    }));
    state = applyMessageStreamEvent(state, event(4, "interrupt.requested", {
      interrupt_request_id: "i1",
      reason: "user",
    }));

    const eventState = activeStateOf(state);
    const snapshotState = activeStateOf(snapshotOf(4, {
      kind: "interrupting",
      phase: "stopping",
      entity_id: "i1",
      status: "stopping",
      last_kind: "model_output",
      last_phase: "text",
      reason: "user",
    }));

    expect(eventState).toEqual({
      kind: "interrupting",
      phase: "stopping",
      entity_id: "i1",
      status: "stopping",
      last_kind: "model_output",
      last_phase: "text",
      reason: "user",
    });
    for (const field of FIELDS) {
      expect(eventState?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("终态 completed：事件路径与快照路径都是 terminal/completed 并附 reason", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "model.started", { model_call_id: "mc_1", attempt: 1 }));
    state = applyMessageStreamEvent(state, event(3, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
    }));
    state = applyMessageStreamEvent(state, event(4, "stream.completed", { status: "completed" }));

    const eventState = activeStateOf(state);
    const snapshotState = activeStateOf(snapshotOf(4, {
      kind: "terminal",
      phase: "completed",
      entity_id: "strm_1",
      status: "completed",
      last_kind: "model_output",
      last_phase: "text",
      reason: "completed",
    }));

    expect(eventState).toEqual({
      kind: "terminal",
      phase: "completed",
      entity_id: "strm_1",
      status: "completed",
      last_kind: "model_output",
      last_phase: "text",
      reason: "completed",
    });
    for (const field of FIELDS) {
      expect(eventState?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("终态 interrupted：事件路径与快照路径都是 terminal/interrupted，last_kind=interrupting", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "model.started", { model_call_id: "mc_1", attempt: 1 }));
    state = applyMessageStreamEvent(state, event(3, "interrupt.requested", {
      interrupt_request_id: "i1",
    }));
    state = applyMessageStreamEvent(state, event(4, "stream.interrupted", {
      interrupt_request_id: "i1",
      status: "interrupted",
    }));

    const eventState = activeStateOf(state);
    const snapshotState = activeStateOf(snapshotOf(4, {
      kind: "terminal",
      phase: "interrupted",
      entity_id: "strm_1",
      status: "interrupted",
      last_kind: "interrupting",
      last_phase: "stopping",
      reason: "interrupted",
    }));

    expect(eventState).toEqual({
      kind: "terminal",
      phase: "interrupted",
      entity_id: "strm_1",
      status: "interrupted",
      last_kind: "interrupting",
      last_phase: "stopping",
      reason: "interrupted",
    });
    for (const field of FIELDS) {
      expect(eventState?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("终态 failed：事件路径与快照路径都是 terminal/failed，reason 取 code", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "model.started", { model_call_id: "mc_1", attempt: 1 }));
    state = applyMessageStreamEvent(state, event(3, "stream.failed", {
      code: "execution_lost",
      message: "boom",
      resumable: false,
    }));

    const eventState = activeStateOf(state);
    const snapshotState = activeStateOf(snapshotOf(3, {
      kind: "terminal",
      phase: "failed",
      entity_id: "strm_1",
      status: "failed",
      last_kind: "model_output",
      last_phase: "reasoning",
      reason: "execution_lost",
    }));

    expect(eventState).toEqual({
      kind: "terminal",
      phase: "failed",
      entity_id: "strm_1",
      status: "failed",
      last_kind: "model_output",
      last_phase: "reasoning",
      reason: "execution_lost",
    });
    for (const field of FIELDS) {
      expect(eventState?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("tool_call 链路：accumulating/stopping 与快照路径逐字段一致", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, {
      ...event(2, "tool_call.delta", {
        tool_call_id: "c1",
        tool_name: "shell",
        arguments: { a: 1 },
        arguments_complete: true,
      }),
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
    });

    expect(activeStateOf(state)).toEqual({
      kind: "tool_call",
      phase: "accumulating",
      entity_id: "c1",
      tool_call_id: "c1",
      status: "accumulating",
    });

    const accumulatingSnapshot = activeStateOf(snapshotOf(2, {
      kind: "tool_call",
      phase: "accumulating",
      entity_id: "c1",
      tool_call_id: "c1",
      status: "accumulating",
    }));
    for (const field of FIELDS) {
      expect(activeStateOf(state)?.[field] ?? null).toBe(accumulatingSnapshot?.[field] ?? null);
    }

    state = applyMessageStreamEvent(state, {
      ...event(3, "tool_call.completed", {
        tool_call_id: "c1",
        tool_invocation_id: "inv1",
        tool_name: "shell",
        status: "completed",
        completion_reason: "tool_started",
        arguments_complete: true,
      }),
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
    });

    expect(activeStateOf(state)).toEqual({
      kind: "tool_call",
      phase: "stopping",
      entity_id: "c1",
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
      status: "completed",
    });

    const stoppingSnapshot = activeStateOf(snapshotOf(3, {
      kind: "tool_call",
      phase: "stopping",
      entity_id: "c1",
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
      status: "completed",
    }));
    for (const field of FIELDS) {
      expect(activeStateOf(state)?.[field] ?? null).toBe(stoppingSnapshot?.[field] ?? null);
    }
  });

  test("tool_execution 链路：running/stopping 与快照路径逐字段一致", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, {
      ...event(2, "tool.started", { tool_name: "shell" }),
      tool_execution_id: "x1",
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
    });

    expect(activeStateOf(state)).toEqual({
      kind: "tool_execution",
      phase: "running",
      entity_id: "x1",
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
      tool_execution_id: "x1",
      status: "running",
    });

    state = applyMessageStreamEvent(state, {
      ...event(3, "tool.completed", {
        tool_name: "shell",
        status: "completed",
        outcome: "success",
      }),
      tool_execution_id: "x1",
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
    });

    expect(activeStateOf(state)).toEqual({
      kind: "tool_execution",
      phase: "stopping",
      entity_id: "x1",
      tool_execution_id: "x1",
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
      status: "completed",
    });

    const snapshotState = activeStateOf(snapshotOf(3, {
      kind: "tool_execution",
      phase: "stopping",
      entity_id: "x1",
      tool_execution_id: "x1",
      tool_call_id: "c1",
      tool_invocation_id: "inv1",
      status: "completed",
    }));
    for (const field of FIELDS) {
      expect(activeStateOf(state)?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("model.completed 与 block.completed 不回写 active_state，与快照保留上一状态一致", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "model.started", { model_call_id: "mc_1", attempt: 1 }));
    state = applyMessageStreamEvent(state, event(3, "model.completed", { model_call_id: "mc_1", attempt: 1 }));
    // 后端 model.completed 分支不回写 active_state，事件侧必须保留 model.started 的取值。
    expect(activeStateOf(state)).toEqual({
      kind: "model_output",
      phase: "reasoning",
      entity_id: "mc_1",
      status: "running",
    });

    state = applyMessageStreamEvent(state, event(4, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
    }));
    state = applyMessageStreamEvent(state, event(5, "block.completed", {
      block_id: "b1",
      status: "completed",
    }));
    // 后端 block.completed 分支同样不回写 active_state。
    expect(activeStateOf(state)).toEqual({
      kind: "model_output",
      phase: "text",
      entity_id: "b1",
      block_id: "b1",
      carrier_type: "text",
      status: "running",
    });
  });
});

describe("block model_call_id 还原", () => {
  // 公共 MessageBlockSnapshot 不带 model_call_id：codec 在快照归一化里把它作为
  // 内部对账字段摘除，而事件路径 upsertBlock 会从事件信封写入它，model.retrying
  // 又据该字段决定把哪些 block 的 projection 置为 "intermediate"。若快照恢复后
  // 该字段恒为 null，同一 Turn 经快照恢复与经事件重放会得到不同 projection，
  // 而 "intermediate" 会让 responseProjection 丢掉该 block 的文本并参与
  // ChatTurn.tsx responsePartsEqual 的 memo 比较。
  const blockFields = [
    "model_call_id",
    "projection",
    "status",
    "completion_reason",
    "partial",
    "carrier_type",
    "text",
  ] as const;

  function snapshotThenRetrying(
    snapshot: Record<string, unknown>,
    seq: number,
  ): MessageStreamState {
    let state = applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", snapshot),
    );
    state = applyMessageStreamEvent(state, event(seq, "model.retrying", {
      model_call_id: snapshot.current_model_call_id,
      attempt: snapshot.current_attempt,
    }));
    return state;
  }

  test("快照恢复后 model.retrying 把当前 call 的 block projection 置为 intermediate", () => {
    // 真实后端线格式：单个 model call 收口后（block 已 completed、projection 仍
    // streaming），随后 model.retrying。快照不带 blocks[].model_call_id，只能由
    // block.started_seq 落在 model_calls[].started_seq 之后还原归属。
    const state = snapshotThenRetrying({
      snapshot_seq: 6,
      stream_status: "open",
      agent_loop_status: "validating",
      current_model_call_id: "mc_1",
      current_attempt: 1,
      blocks: [{
        block_id: "mc_1:block:text_1",
        block_index: 0,
        items: [],
        status: "completed",
        carrier_type: "text",
        projection: "streaming",
        text: "第一轮正文",
        completion_reason: "upstream_completed",
        partial: false,
        started_seq: 3,
        last_event_seq: 5,
        completed_seq: 5,
      }],
      model_calls: [{
        model_call_id: "mc_1",
        attempt: 1,
        status: "completed",
        started_seq: 2,
        last_event_seq: 6,
        completed_seq: 6,
      }],
      resumable: true,
    }, 7);

    expect(state.blocks[0]?.model_call_id).toBe("mc_1");
    expect(state.blocks[0]?.projection).toBe("intermediate");
  });

  test("快照恢复后 model.retrying 不命中属于上一个 call 的 block", () => {
    // 工具循环：mc_1 的 block 已收口，mc_2 的 block 运行中且是 current。retrying
    // 只应标记 mc_2 的 block；mc_1 的旧文本必须继续作为最终答复保留。
    const state = snapshotThenRetrying({
      snapshot_seq: 12,
      stream_status: "open",
      agent_loop_status: "validating",
      current_model_call_id: "mc_2",
      current_attempt: 2,
      blocks: [
        {
          block_id: "mc_1:block:text_1",
          block_index: 0,
          items: [],
          status: "completed",
          carrier_type: "text",
          projection: "streaming",
          text: "第一轮最终文本",
          completion_reason: "upstream_completed",
          partial: false,
          started_seq: 3,
          last_event_seq: 5,
          completed_seq: 5,
        },
        {
          block_id: "mc_2:block:text_1",
          block_index: 1,
          items: [],
          status: "running",
          carrier_type: "text",
          projection: "streaming",
          text: "第二轮正文",
          started_seq: 9,
          last_event_seq: 11,
        },
      ],
      model_calls: [
        { model_call_id: "mc_1", attempt: 1, status: "completed", started_seq: 2, last_event_seq: 7, completed_seq: 7 },
        { model_call_id: "mc_2", attempt: 2, status: "completed", started_seq: 8, last_event_seq: 12, completed_seq: 12 },
      ],
      resumable: true,
    }, 13);

    expect(state.blocks[0]?.model_call_id).toBe("mc_1");
    expect(state.blocks[0]?.projection).toBe("streaming");
    expect(state.blocks[1]?.model_call_id).toBe("mc_2");
    expect(state.blocks[1]?.projection).toBe("intermediate");
  });

  test("乱序前置段的 block 归入其后第一个 model call", () => {
    // provider delta 可能先于所属 call 的 model.started 提交：block 的 started_seq
    // 落在上一个 call 收口之后、本 call started 之前的空隙，后端仍把它记为
    // 本 call 归属，因此必须归入其后第一个 call 而不是上一个。
    const state = snapshotThenRetrying({
      snapshot_seq: 11,
      stream_status: "open",
      agent_loop_status: "validating",
      current_model_call_id: "mc_2",
      current_attempt: 2,
      blocks: [
        {
          block_id: "mc_1:block:text_1",
          block_index: 0,
          items: [],
          status: "completed",
          carrier_type: "text",
          projection: "streaming",
          text: "mc1 正文",
          completion_reason: "upstream_completed",
          partial: false,
          started_seq: 3,
          last_event_seq: 5,
          completed_seq: 5,
        },
        {
          block_id: "mc_2:block:text_1",
          block_index: 1,
          items: [],
          status: "completed",
          carrier_type: "text",
          projection: "streaming",
          text: "mc2 前置段",
          completion_reason: "upstream_completed",
          partial: false,
          started_seq: 7,
          last_event_seq: 10,
          completed_seq: 10,
        },
      ],
      model_calls: [
        { model_call_id: "mc_1", attempt: 1, status: "completed", started_seq: 2, last_event_seq: 6, completed_seq: 6 },
        { model_call_id: "mc_2", attempt: 2, status: "completed", started_seq: 9, last_event_seq: 11, completed_seq: 11 },
      ],
      resumable: true,
    }, 12);

    expect(state.blocks[0]?.model_call_id).toBe("mc_1");
    expect(state.blocks[0]?.projection).toBe("streaming");
    expect(state.blocks[1]?.model_call_id).toBe("mc_2");
    expect(state.blocks[1]?.projection).toBe("intermediate");
  });

  test("末次 model call 之后仍 running 的 block 归入当前 call", () => {
    // 后端把末次 call 之后的迟到事件重映射到 current_model_call_id，快照侧同样兜底。
    const state = snapshotThenRetrying({
      snapshot_seq: 7,
      stream_status: "open",
      agent_loop_status: "retrying",
      current_model_call_id: "mc_1",
      current_attempt: 1,
      blocks: [{
        block_id: "mc_1:block:text_1",
        block_index: 1,
        items: [],
        status: "running",
        carrier_type: "text",
        projection: "streaming",
        text: "运行中正文",
        started_seq: 7,
        last_event_seq: 8,
      }],
      model_calls: [{
        model_call_id: "mc_1",
        attempt: 1,
        status: "completed",
        started_seq: 2,
        last_event_seq: 6,
        completed_seq: 6,
      }],
      resumable: true,
    }, 8);

    expect(state.blocks[0]?.model_call_id).toBe("mc_1");
    expect(state.blocks[0]?.projection).toBe("intermediate");
  });

  test("快照路径与事件路径对 retrying 后 block 的投影逐字段一致", () => {
    // 单 call：快照在 model.completed(validation_failed) 之后、model.retrying 之前取得；
    // 事件路径从 stream.opened 全量重放。两条链路必须给出同一份 block 事实与部件。
    const snapshotState = snapshotThenRetrying({
      snapshot_seq: 6,
      stream_status: "open",
      agent_loop_status: "validating",
      current_model_call_id: "mc_1",
      current_attempt: 1,
      blocks: [{
        block_id: "mc_1:block:text_1",
        block_index: 0,
        items: [],
        status: "completed",
        carrier_type: "text",
        projection: "streaming",
        text: "被校验拒绝的正文",
        completion_reason: "upstream_completed",
        partial: false,
        started_seq: 3,
        last_event_seq: 5,
        completed_seq: 5,
      }],
      model_calls: [{
        model_call_id: "mc_1",
        attempt: 1,
        status: "completed",
        started_seq: 2,
        last_event_seq: 6,
        completed_seq: 6,
      }],
      resumable: true,
    }, 7);

    let eventState = createMessageStreamState("ses_1", "turn_1");
    eventState = applyMessageStreamEvent(eventState, event(1, "stream.opened", { status: "open" }));
    eventState = applyMessageStreamEvent(eventState, event(2, "model.started", {
      model_call_id: "mc_1",
      attempt: 1,
    }));
    eventState = applyMessageStreamEvent(eventState, event(3, "block.started", {
      block_id: "mc_1:block:text_1",
      block_index: 0,
      carrier_type: "text",
      projection: "streaming",
      model_call_id: "mc_1",
    }));
    eventState = applyMessageStreamEvent(eventState, event(4, "block.delta", {
      block_id: "mc_1:block:text_1",
      operation: "append",
      text: "被校验拒绝的正文",
    }));
    eventState = applyMessageStreamEvent(eventState, event(5, "block.completed", {
      block_id: "mc_1:block:text_1",
      status: "completed",
      completion_reason: "upstream_completed",
      partial: false,
    }));
    eventState = applyMessageStreamEvent(eventState, event(6, "model.completed", {
      model_call_id: "mc_1",
      attempt: 1,
      outcome: "validation_failed",
    }));
    eventState = applyMessageStreamEvent(eventState, event(7, "model.retrying", {
      model_call_id: "mc_1",
      attempt: 1,
      reason: "校验未通过",
    }));

    const snapshotBlock = snapshotState.blocks[0];
    const eventBlock = eventState.blocks[0];
    expect(snapshotBlock?.projection).toBe("intermediate");
    expect(eventBlock?.projection).toBe("intermediate");
    for (const field of blockFields) {
      expect(snapshotBlock?.[field]).toBe(eventBlock?.[field]);
    }
    expect(messageStreamToResponseParts(snapshotState)).toEqual(
      messageStreamToResponseParts(eventState),
    );
  });
});

describe("active_state 越界写入收口", () => {
  // 后端 store.py 的 block.delta 分支只调 _apply_block_delta，只有 block.started
  // 写 active_state。前端 applyBlockDelta 原先无条件覆写 active_state，一旦 provider
  // 在同一 model call 内于 on_tool_start 之后继续吐正文（真实 runtime 可复现：
  // T->S->T2 产出 block.started/block.delta/tool_call.completed/tool.started/block.delta），
  // 这条迟到的 block.delta 会把 active_state 从 tool_execution 拉回 model_output，
  // 而同一时点的后端快照仍是 tool_execution，两条链路给出不同 UI。
  const FIELDS = [
    "kind",
    "phase",
    "entity_id",
    "block_id",
    "carrier_type",
    "tool_call_id",
    "tool_execution_id",
    "tool_invocation_id",
    "tool_attempt_id",
    "status",
    "last_kind",
    "last_phase",
    "reason",
  ] as const;

  function activeStateOf(state: MessageStreamState) {
    return state.activeState;
  }

  function snapshotOf(seq: number, activeState: Record<string, unknown>) {
    return applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(seq, "stream.snapshot", {
        snapshot_seq: seq,
        stream_status: "open",
        agent_loop_status: "tool_running",
        current_attempt: 1,
        active_state: activeState,
        resumable: true,
      }),
    );
  }

  test("迟到的 block.delta 不把 active_state 从 tool_execution 拉回 model_output", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "model.started", {
      model_call_id: "mc_1",
      attempt: 1,
    }));
    state = applyMessageStreamEvent(state, event(3, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
      projection: "streaming",
    }));
    state = applyMessageStreamEvent(state, event(4, "tool.started", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "shell",
    }));
    // 工具已启动后，同一 block 的正文 delta 才到。
    state = applyMessageStreamEvent(state, event(5, "block.delta", {
      block_id: "b1",
      operation: "append",
      text: "迟到正文",
    }));

    // 正文必须照常累积，active_state 必须保持工具执行态。
    expect(state.blocks[0]?.text).toBe("迟到正文");
    expect(activeStateOf(state)).toEqual({
      kind: "tool_execution",
      phase: "running",
      entity_id: "exec_1",
      tool_call_id: "call_1",
      tool_execution_id: "exec_1",
      status: "running",
    });

    const snapshotState = activeStateOf(snapshotOf(5, {
      kind: "tool_execution",
      phase: "running",
      entity_id: "exec_1",
      tool_call_id: "call_1",
      tool_execution_id: "exec_1",
      status: "running",
    }));
    for (const field of FIELDS) {
      expect(activeStateOf(state)?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("tool_call 之后的迟到 block.delta 同样不覆写 active_state", () => {
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "model.started", {
      model_call_id: "mc_1",
      attempt: 1,
    }));
    state = applyMessageStreamEvent(state, event(3, "block.started", {
      block_id: "b1",
      block_index: 0,
      carrier_type: "text",
      projection: "streaming",
    }));
    state = applyMessageStreamEvent(state, event(4, "tool_call.delta", {
      tool_call_id: "c1",
      tool_name: "shell",
      arguments: { a: 1 },
    }));
    state = applyMessageStreamEvent(state, event(5, "block.delta", {
      block_id: "b1",
      operation: "append",
      text: "又一段正文",
    }));

    expect(activeStateOf(state)).toEqual({
      kind: "tool_call",
      phase: "accumulating",
      entity_id: "c1",
      tool_call_id: "c1",
      status: "accumulating",
    });

    const snapshotState = activeStateOf(snapshotOf(5, {
      kind: "tool_call",
      phase: "accumulating",
      entity_id: "c1",
      tool_call_id: "c1",
      status: "accumulating",
    }));
    for (const field of FIELDS) {
      expect(activeStateOf(state)?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("block.started 仍然写入 active_state，与后端同一分支一致", () => {
    // 收口只删除 block.delta 的越界写入，block.started 的写入必须保留。
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, event(1, "stream.opened", { status: "open" }));
    state = applyMessageStreamEvent(state, event(2, "tool.started", {
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_name: "shell",
    }));
    state = applyMessageStreamEvent(state, event(3, "block.started", {
      block_id: "b2",
      block_index: 1,
      carrier_type: "reasoning",
      projection: "streaming",
    }));

    expect(activeStateOf(state)).toEqual({
      kind: "model_output",
      phase: "reasoning",
      entity_id: "b2",
      block_id: "b2",
      carrier_type: "reasoning",
      status: "running",
    });

    const snapshotState = activeStateOf(snapshotOf(3, {
      kind: "model_output",
      phase: "reasoning",
      entity_id: "b2",
      block_id: "b2",
      carrier_type: "reasoning",
      status: "running",
    }));
    for (const field of FIELDS) {
      expect(activeStateOf(state)?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
  });

  test("tool 链路的身份归一只有一处：payload 缺失时实体与 active_state 同源", () => {
    // payload 只带 tool_name，身份仅出现在信封；实体与 active_state 必须得到
    // 同一份补全结果，且与后端把身份写进 active_state 的行为一致。
    let state = createMessageStreamState("ses_1", "turn_1");
    state = applyMessageStreamEvent(state, {
      ...event(1, "tool.started", { tool_name: "shell" }),
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "inv_1",
      tool_attempt_id: "att_1",
    });
    expect(state.toolExecutions[0]).toMatchObject({
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "inv_1",
      tool_attempt_id: "att_1",
    });

    const execution = state.toolExecutions[0];
    const snapshotState = activeStateOf(snapshotOf(1, {
      kind: "tool_execution",
      phase: "running",
      entity_id: "exec_1",
      tool_execution_id: "exec_1",
      tool_call_id: "call_1",
      tool_invocation_id: "inv_1",
      tool_attempt_id: "att_1",
      status: "running",
    }));
    for (const field of FIELDS) {
      expect(activeStateOf(state)?.[field] ?? null).toBe(snapshotState?.[field] ?? null);
    }
    expect(execution?.tool_attempt_id).toBe("att_1");
  });
});

describe("终态白名单同域去重", () => {
  // streamStatus 域（completed/interrupted/failed）的终态判定只允许存在唯一实现：
  // state/messageStream/state.ts 的 isTerminalStatus。conversations.ts 曾有一份逐字
  // 相同的 isTerminalMessageStreamStatus。两份实现一旦取值漂移，会话选择排序、活动
  // 遮罩与 protocolError 归一就会与消息流连接状态判定分叉；这类行为型断言在只有一份
  // 实现时无法发现重复回归，因此这里直接以源码级守卫钉住唯一实现。
  // 注意：isTerminalEvent（事件类型域）与 TERMINAL_TURN_STATUSES（Turn 状态域，5 值）
  // 是不同语义域，不在本去重范围内。

  test("isTerminalStatus 覆盖且仅覆盖 streamStatus 的 3 个终态", () => {
    const domain = ["open", "interrupting", "completed", "interrupted", "failed"] as const;
    const terminal = domain.filter((status) => isTerminalStatus(status));
    expect(terminal).toEqual(["completed", "interrupted", "failed"]);
  });

  test("conversations.ts 不再保留 streamStatus 域的第二套终态实现", async () => {
    const source = await Bun.file(
      new URL("../conversations.ts", import.meta.url),
    ).text();
    expect(source).not.toContain("isTerminalMessageStreamStatus");
    // 必须复用唯一的 isTerminalStatus，而不是重新手写三个字面量比较。
    expect(source).toContain('from "./messageStream/state"');
    expect(source).toContain("isTerminalStatus(");
    // 不同语义域的常量必须保留，不得被本次去重误删。
    expect(source).toContain("TERMINAL_TURN_STATUSES");
  });
});

describe("block model_call_id 归属真源", () => {
  // 归属真源是 block_id 前缀：后端 _scoped_block_id 恒定构造
  // `${model_call_id or "unbound-model-call"}:block:${provider_block_id}`，与
  // block.started 事件信封写入的 model_call_id 恒等。事件序号区间无法表达后端
  // _resolve_model_call_id 的真实语义：同一段空隙里，带新 call 显式身份的 delta
  // 归其后第一个 call，带旧 call 身份或空身份的 delta 归 current_model_call_id，
  // 两种情形 block 的 started_seq 形态完全相同（见下方两条空隙用例）。

  function snapshotState(snapshot: Record<string, unknown>): MessageStreamState {
    return applyMessageStreamEvent(
      createMessageStreamState("ses_1", "turn_1"),
      event(1, "stream.snapshot", snapshot),
    );
  }

  function blockBy(blockId: string, snapshot: Record<string, unknown>) {
    return snapshotState(snapshot).blocks.find((block) => block.block_id === blockId);
  }

  test("unbound-model-call 前缀的 block 还原为 null", () => {
    // provider delta 在任何 model.started 之前落盘：后端用 "unbound-model-call"
    // 兜底 scoped id，事件路径的 model_call_id 也就是 null。即便 block 的
    // started_seq 早于 mc_1 区间、且 current 已是 mc_1，也绝不能猜测成 mc_1。
    const snapshot = {
      snapshot_seq: 2,
      stream_status: "open",
      agent_loop_status: "model_running",
      current_model_call_id: "mc_1",
      current_attempt: 1,
      blocks: [{
        block_id: "unbound-model-call:block:text_early",
        block_index: 0,
        items: [],
        status: "completed",
        carrier_type: "text",
        projection: "streaming",
        text: "无归属前置段",
        completion_reason: "upstream_completed",
        partial: false,
        started_seq: 2,
        last_event_seq: 3,
        completed_seq: 3,
      }],
      model_calls: [{
        model_call_id: "mc_1",
        attempt: 1,
        status: "completed",
        started_seq: 4,
        last_event_seq: 5,
        completed_seq: 5,
      }],
      resumable: true,
    };

    const block = blockBy("unbound-model-call:block:text_early", snapshot);
    expect(block?.model_call_id).toBeNull();
    expect(block?.projection).toBe("streaming");
  });

  test("空隙块复用旧 call 身份时归 current，而不是其后第一个 call", () => {
    // 真实后端事实（s9）：mc_1 收口后、mc_2 start 前，复用 mc_1 身份的迟到新 block。
    // 块 started_seq=7 落在 mc_1.completed=6 与 mc_2.started=9 之间的空隙；后端归
    // mc_1（current）。旧序号规则会错归其后第一个 call mc_2，进而把 mc_1 正文标成
    // intermediate 并从 projection 丢弃。
    const snapshot = {
      snapshot_seq: 15,
      stream_status: "open",
      agent_loop_status: "retrying",
      current_model_call_id: "mc_2",
      current_attempt: 2,
      blocks: [
        {
          block_id: "mc_1:block:text_late",
          block_index: 1,
          items: [],
          status: "completed",
          carrier_type: "text",
          projection: "streaming",
          text: "mc1 收口后迟到新 block",
          completion_reason: "upstream_completed",
          partial: false,
          started_seq: 7,
          last_event_seq: 10,
          completed_seq: 10,
        },
        {
          block_id: "mc_2:block:text_2",
          block_index: 2,
          items: [],
          status: "completed",
          carrier_type: "text",
          projection: "streaming",
          text: "mc2 正文",
          completion_reason: "upstream_completed",
          partial: false,
          started_seq: 11,
          last_event_seq: 13,
          completed_seq: 13,
        },
      ],
      model_calls: [
        { model_call_id: "mc_1", attempt: 1, status: "completed", started_seq: 2, last_event_seq: 6, completed_seq: 6 },
        { model_call_id: "mc_2", attempt: 2, status: "completed", started_seq: 9, last_event_seq: 14, completed_seq: 14 },
      ],
      resumable: true,
    };

    expect(blockBy("mc_1:block:text_late", snapshot)?.model_call_id).toBe("mc_1");
    expect(blockBy("mc_2:block:text_2", snapshot)?.model_call_id).toBe("mc_2");
  });

  test("乱序 model_calls 输入不影响归属", () => {
    // model_calls 数组顺序不保证按 started_seq 升序；归属只取决于 block_id 前缀。
    const snapshot = {
      snapshot_seq: 15,
      stream_status: "open",
      agent_loop_status: "retrying",
      current_model_call_id: "mc_2",
      current_attempt: 2,
      blocks: [
        {
          block_id: "mc_1:block:text_1",
          block_index: 0,
          items: [],
          status: "completed",
          carrier_type: "text",
          projection: "streaming",
          text: "mc1 正文",
          completion_reason: "upstream_completed",
          partial: false,
          started_seq: 3,
          last_event_seq: 5,
          completed_seq: 5,
        },
        {
          block_id: "mc_2:block:text_2",
          block_index: 1,
          items: [],
          status: "running",
          carrier_type: "text",
          projection: "streaming",
          text: "mc2 正文",
          started_seq: 11,
          last_event_seq: 13,
        },
      ],
      // 故意逆序：mc_2 在前、mc_1 在后，且 mc_2 的 started_seq 更大。
      model_calls: [
        { model_call_id: "mc_2", attempt: 2, status: "completed", started_seq: 9, last_event_seq: 14, completed_seq: 14 },
        { model_call_id: "mc_1", attempt: 1, status: "completed", started_seq: 2, last_event_seq: 6, completed_seq: 6 },
      ],
      resumable: true,
    };

    expect(blockBy("mc_1:block:text_1", snapshot)?.model_call_id).toBe("mc_1");
    expect(blockBy("mc_2:block:text_2", snapshot)?.model_call_id).toBe("mc_2");
  });

  test("缺 ':block:' 分隔符的非法身份不猜测归属", () => {
    // 后端 block_id 恒含 ':block:'；缺失说明身份非法，必须返回 null 而不是用
    // started_seq 或 current 猜测。
    const snapshot = {
      snapshot_seq: 6,
      stream_status: "open",
      agent_loop_status: "validating",
      current_model_call_id: "mc_1",
      current_attempt: 1,
      blocks: [{
        block_id: "legacy-block-id",
        block_index: 0,
        items: [],
        status: "completed",
        carrier_type: "text",
        projection: "streaming",
        text: "旧格式身份",
        completion_reason: "upstream_completed",
        partial: false,
        started_seq: 3,
        last_event_seq: 5,
        completed_seq: 5,
      }],
      model_calls: [{
        model_call_id: "mc_1",
        attempt: 1,
        status: "completed",
        started_seq: 2,
        last_event_seq: 6,
        completed_seq: 6,
      }],
      resumable: true,
    };

    expect(blockBy("legacy-block-id", snapshot)?.model_call_id).toBeNull();
  });
});
