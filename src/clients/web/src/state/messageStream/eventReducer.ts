// SSE 数据事件 reducer：按 event_seq 推进消息流状态，维护实体 upsert 与终态收口。
// 与 snapshotHydration 互为递归：乱序事件需要快照兜底，快照恢复后需要重放缓冲事件。
import {
  finishRunningActivities,
  finishRunningModelCalls,
  interruptingActiveState,
  modelOutputPhase,
  terminalActiveState,
  upsertActivity,
  upsertModelCall,
} from "./activityReducer";
import { applySnapshotState } from "./snapshotHydration";
import {
  MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS,
  MESSAGE_STREAM_PENDING_EVENT_LIMIT,
  applyLifecycle,
  blockStatusValue,
  booleanValue,
  boundedMessageStreamText,
  cloneMessageStreamState,
  createMessageStreamState,
  defaultedTextValue,
  failureFromValue,
  findBlock,
  isRecord,
  isTerminalStatus,
  lifecycleFromValue,
  numberValue,
  optionalTextValue,
  stringValue,
  toolExecutionStatusValue,
} from "./state";
import {
  finishRunningToolCalls,
  markRunningToolsUnknown,
  mergeToolCall,
  upsertTool,
  withToolIdentityFallback,
} from "./toolReducer";
import type {
  MessageStreamBlock,
  MessageStreamEvent,
  MessageStreamEventType,
  MessageStreamState,
} from "./types";

export function applyMessageStreamEvent(
  current: MessageStreamState | null,
  event: MessageStreamEvent,
): MessageStreamState {
  const state = current
    ? cloneMessageStreamState(current)
    : createMessageStreamState(event.session_id, event.turn_id);
  if (state.sessionId !== event.session_id || state.turnId !== event.turn_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流关联键与当前 Turn 不一致",
    };
  }
  if (state.workspaceId && event.workspace_id && state.workspaceId !== event.workspace_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流 workspace_id 与当前工作区不一致",
    };
  }
  if (state.turnStreamId && state.turnStreamId !== event.turn_stream_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流 turn_stream_id 在同一 Turn 内发生变化",
    };
  }
  // 终态互斥且幂等：completed/interrupted/failed 一旦确定，后续业务事件一律不得
  // 改写 streamStatus、failure 或连接镜像，否则第二个终态会覆盖第一个，乱序或重复
  // 帧还会把连接状态翻回 connected。后端在同一状态机上以 MessageStreamTerminalError
  // 拒绝终态后的业务事件，只放行 interrupt.rejected 与 stream.snapshot 两类控制帧
  // （app/services/infrastructure/message_stream_store.py:1106），前端镜像同一准入集合。
  // interrupt.rejected 是「中断请求在终态后到达」的可见反馈；快照控制帧仍是恢复权威，
  // 二者都必须继续走各自分支，其余事件直接丢弃。
  if (
    isTerminalStatus(state.streamStatus)
    && event.type !== "interrupt.rejected"
    && event.type !== "stream.snapshot"
  ) {
    return state;
  }
  if (event.type === "stream.snapshot") {
    if (event.payload.snapshot_seq < state.lastEventSeq) return state;
    return drainPendingEvents(applySnapshotState(state, event.payload, event));
  }
  if (event.event_seq <= state.lastEventSeq) return state;
  if (event.event_seq !== state.lastEventSeq + 1) {
    const pendingEvents = state.pendingEvents.filter(
      (pending) => pending.event_id !== event.event_id,
    );
    pendingEvents.push(event);
    pendingEvents.sort((left, right) => left.event_seq - right.event_seq);
    const overflowed = pendingEvents.length > MESSAGE_STREAM_PENDING_EVENT_LIMIT;
    if (overflowed) {
      pendingEvents.length = MESSAGE_STREAM_PENDING_EVENT_LIMIT;
    }
    return {
      ...state,
      connectionStatus: "gap",
      pendingEvents,
      protocolError: overflowed
        ? `消息流乱序事件超过 ${MESSAGE_STREAM_PENDING_EVENT_LIMIT} 条，必须通过 snapshot 恢复`
        : `消息流 event_seq 不连续: expected=${state.lastEventSeq + 1} actual=${event.event_seq}`,
    };
  }

  state.lastEventSeq = event.event_seq;
  state.workspaceId = event.workspace_id ?? state.workspaceId;
  state.turnStreamId = event.turn_stream_id;
  // 连接镜像同样只由终态决定：上一步放行的 interrupt.rejected 落在已终态的消息流上
  // 时，不得把连接状态重开。
  state.connectionStatus = isTerminalStatus(state.streamStatus) || isTerminalEvent(event.type)
    ? "terminal"
    : "connected";
  state.protocolError = null;
  const payload = event.payload;
  switch (event.type) {
    case "stream.opened":
      state.streamStatus = "open";
      break;
    case "model.started":
      state.currentModelCallId = stringValue(payload.model_call_id) ?? event.model_call_id ?? null;
      state.currentAttempt = numberValue(payload.attempt) ?? state.currentAttempt;
      state.agentLoopStatus = "model_running";
      upsertModelCall(
        state,
        event.model_call_id && !payload.model_call_id
          ? { ...payload, model_call_id: event.model_call_id }
          : payload,
        "running",
        event,
      );
      state.activeState = {
        kind: "model_output",
        phase: "reasoning",
        entity_id: state.currentModelCallId ?? "",
        status: "running",
      };
      break;
    case "model.completed":
      state.agentLoopStatus = "validating";
      upsertModelCall(
        state,
        event.model_call_id && !payload.model_call_id
          ? { ...payload, model_call_id: event.model_call_id }
          : payload,
        "completed",
        event,
      );
      // 后端 model.completed 分支不回写 active_state（store.py:1317-1325），快照侧
      // 因此保留 model.started 的取值；事件侧同样不得发明 validating 阶段。
      break;
    case "model.retrying":
      state.agentLoopStatus = "retrying";
      for (const block of state.blocks) {
        if (block.model_call_id === state.currentModelCallId) {
          block.projection = "intermediate";
        }
      }
      break;
    case "model.failed":
      state.agentLoopStatus = "failed";
      state.failure = failureFromPayload(payload);
      upsertModelCall(
        state,
        event.model_call_id && !payload.model_call_id
          ? { ...payload, model_call_id: event.model_call_id }
          : payload,
        "failed",
        event,
      );
      break;
    case "block.started":
      {
        const blockPayload = { ...payload };
        if (event.block_id && !blockPayload.block_id) {
          blockPayload.block_id = event.block_id;
        }
        if (event.model_call_id && !blockPayload.model_call_id) {
          blockPayload.model_call_id = event.model_call_id;
        }
        const block = upsertBlock(
          state,
          blockPayload,
          "running",
          event,
        );
        if (block) {
          state.activeState = {
            kind: "model_output",
            phase: modelOutputPhase(block.carrier_type),
            entity_id: block.block_id,
            block_id: block.block_id,
            carrier_type: block.carrier_type,
            status: "running",
          };
        }
      }
      break;
    case "block.delta":
      applyBlockDelta(
        state,
        event.block_id && !payload.block_id
          ? { ...payload, block_id: event.block_id }
          : payload,
        event,
      );
      break;
    case "block.completed": {
      const block = findBlock(state, stringValue(payload.block_id) ?? event.block_id ?? null);
      if (block) {
        block.status = blockStatusValue(payload.status);
        block.completion_reason = defaultedTextValue(payload.completion_reason, "upstream_completed");
        block.partial = booleanValue(payload.partial) ?? false;
        applyLifecycle(block, event, true);
      }
      break;
    }
    case "tool_call":
    case "tool_call.delta": {
      const toolCallId = stringValue(payload.tool_call_id) ?? event.tool_call_id;
      if (toolCallId) {
        const toolCallPayload = withToolIdentityFallback(payload, event);
        state.toolCalls[toolCallId] = mergeToolCall(
          state.toolCalls[toolCallId],
          toolCallPayload,
        );
        applyLifecycle(state.toolCalls[toolCallId], event);
        // 逐字段对齐后端 tool_call / tool_call.delta 分支：phase 恒为 accumulating，
        // status 取 payload（tool_call.delta 的公共线格式刻意剥离 status，缺失时
        // 按 accumulating 归一），归属身份只取 payload，不额外补信封身份。
        state.activeState = {
          kind: "tool_call",
          phase: "accumulating",
          entity_id: toolCallId,
          tool_call_id: toolCallId,
          tool_invocation_id: stringValue(payload.tool_invocation_id) ?? undefined,
          tool_attempt_id: stringValue(payload.tool_attempt_id) ?? undefined,
          status: stringValue(payload.status) ?? "accumulating",
        };
      }
      break;
    }
    case "tool_call.completed": {
      const toolCallId = stringValue(payload.tool_call_id) ?? event.tool_call_id;
      if (toolCallId) {
        const toolCallPayload = withToolIdentityFallback(payload, event);
        state.toolCalls[toolCallId] = mergeToolCall(
          state.toolCalls[toolCallId],
          toolCallPayload,
        );
        applyLifecycle(state.toolCalls[toolCallId], event, true);
        // 后端 tool_call.completed 用 phase "stopping"，缺省 status "incomplete"；
        // 归属身份同样只取 payload，不额外补信封身份。
        state.activeState = {
          kind: "tool_call",
          phase: "stopping",
          entity_id: toolCallId,
          tool_call_id: toolCallId,
          tool_invocation_id: stringValue(payload.tool_invocation_id) ?? undefined,
          tool_attempt_id: stringValue(payload.tool_attempt_id) ?? undefined,
          status: stringValue(payload.status) ?? "incomplete",
        };
      }
      break;
    }
    case "tool.started":
      {
        // 身份归一只有一处：实体与 active_state 共用同一份补全结果，避免
        // 同一个「payload 缺失时补信封身份」的表达式在前端出现第二份实现。
        const toolPayload = withToolIdentityFallback(payload, event);
        upsertTool(state, toolPayload, "running", event);
        state.agentLoopStatus = "tool_running";
        state.activeState = {
          kind: "tool_execution",
          phase: "running",
          entity_id: stringValue(toolPayload.tool_execution_id) ?? "",
          tool_call_id: stringValue(toolPayload.tool_call_id) ?? undefined,
          tool_invocation_id: stringValue(toolPayload.tool_invocation_id) ?? undefined,
          tool_attempt_id: stringValue(toolPayload.tool_attempt_id) ?? undefined,
          tool_execution_id: stringValue(toolPayload.tool_execution_id) ?? undefined,
          status: "running",
        };
      }
      break;
    case "tool.completed":
      {
        const toolPayload = withToolIdentityFallback(payload, event);
        upsertTool(
          state,
          toolPayload,
          toolExecutionStatusValue(payload.status),
          event,
        );
        // 后端 tool.completed 用 phase "stopping"，并带上执行的三个归属身份。
        state.activeState = {
          kind: "tool_execution",
          phase: "stopping",
          entity_id: stringValue(toolPayload.tool_execution_id) ?? "",
          tool_execution_id: stringValue(toolPayload.tool_execution_id) ?? undefined,
          tool_call_id: stringValue(toolPayload.tool_call_id) ?? undefined,
          tool_invocation_id: stringValue(toolPayload.tool_invocation_id) ?? undefined,
          tool_attempt_id: stringValue(toolPayload.tool_attempt_id) ?? undefined,
          status: toolExecutionStatusValue(payload.status),
        };
      }
      break;
    case "activity.started":
    case "activity.updated":
    case "activity.completed":
    case "activity.failed":
      upsertActivity(state, payload, event);
      break;
    case "interrupt.requested":
      state.streamStatus = "interrupting";
      state.activeState = interruptingActiveState(
        state.activeState,
        stringValue(payload.interrupt_request_id) ?? "",
        optionalTextValue(payload.reason),
      );
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "requested",
        reason: optionalTextValue(payload.reason),
        factConfirmed: false,
      };
      break;
    case "interrupt.rejected":
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "rejected",
        reason: optionalTextValue(payload.reason),
        factConfirmed: false,
      };
      break;
    case "stream.completed":
      state.streamStatus = "completed";
      state.agentLoopStatus = "completed";
      state.resumable = false;
      state.activeState = terminalActiveState(
        state.activeState,
        state.turnStreamId,
        "completed",
        terminalReason(payload, "completed"),
      );
      break;
    case "stream.interrupted":
      state.streamStatus = "interrupted";
      state.agentLoopStatus = "interrupted";
      state.resumable = false;
      finishRunningModelCalls(state, event, "user_interrupt");
      finishRunningBlocks(state, "interrupted", event);
      finishRunningToolCalls(state, event, "user_interrupt");
      markRunningToolsUnknown(state, event);
      finishRunningActivities(state, event, "user_interrupt");
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "confirmed",
        factConfirmed: true,
      };
      state.activeState = terminalActiveState(
        state.activeState,
        state.turnStreamId,
        "interrupted",
        terminalReason(payload, "interrupted"),
      );
      break;
    case "stream.failed":
      state.streamStatus = "failed";
      state.agentLoopStatus = "failed";
      state.failure = failureFromPayload(payload);
      state.resumable = booleanValue(payload.resumable) ?? false;
      finishRunningModelCalls(state, event, "execution_lost");
      finishRunningBlocks(state, "failed", event);
      finishRunningToolCalls(state, event, "execution_lost");
      markRunningToolsUnknown(state, event);
      finishRunningActivities(state, event, "execution_lost");
      state.activeState = terminalActiveState(
        state.activeState,
        state.turnStreamId,
        "failed",
        terminalReason(payload, "failed"),
      );
      break;
  }
  return drainPendingEvents(state);
}

export function drainPendingEvents(state: MessageStreamState): MessageStreamState {
  if (state.pendingEvents.length === 0) return state;
  let next: MessageStreamState = { ...state, pendingEvents: [] };
  for (const pending of [...state.pendingEvents].sort((left, right) => left.event_seq - right.event_seq)) {
    // 缓冲按 event_seq 回放时，终态仍是硬边界：后端拒绝终态后的新业务事件，
    // 因此回放中一旦收口，剩余的更高序号事件在协议上不可达，既不能再应用，
    // 也不能留在 pendingEvents 里假装还有待补齐的缺口。
    if (isTerminalStatus(next.streamStatus)) {
      break;
    }
    if (pending.event_seq === next.lastEventSeq + 1) {
      next = applyMessageStreamEvent(next, pending);
    } else if (pending.event_seq > next.lastEventSeq) {
      next.pendingEvents.push(pending);
    }
  }
  if (next.pendingEvents.length === 0 && next.connectionStatus === "gap") {
    next.connectionStatus = isTerminalStatus(next.streamStatus) ? "terminal" : "connected";
    next.protocolError = null;
  }
  return next;
}

function upsertBlock(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  status: MessageStreamBlock["status"],
  event: MessageStreamEvent,
): MessageStreamBlock | null {
  const blockId = stringValue(payload.block_id);
  if (!blockId) return null;
  const existing = findBlock(state, blockId);
  if (existing) {
    existing.status = status;
    applyLifecycle(existing, event);
    return existing;
  }
  const block: MessageStreamBlock = {
    block_id: blockId,
    model_call_id: stringValue(payload.model_call_id),
    block_index: numberValue(payload.block_index) ?? state.blocks.length,
    carrier_type: defaultedTextValue(payload.carrier_type, "text"),
    status,
    text: "",
    items: [],
    redacted: booleanValue(payload.redacted) ?? false,
    projection: defaultedTextValue(payload.projection, "streaming"),
    completion_reason: optionalTextValue(payload.completion_reason),
    partial: booleanValue(payload.partial) ?? false,
    ...lifecycleFromValue(payload),
  };
  state.blocks.push(block);
  applyLifecycle(block, event);
  return block;
}

function applyBlockDelta(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  event: MessageStreamEvent,
): void {
  const block = upsertBlock(state, payload, "running", event);
  if (!block) return;
  // block.delta 不改写 active_state：后端 store.py 的 block.delta 分支只调
  // _apply_block_delta，只有 block.started 才写 active_state。若这里覆写，
  // 一条迟到的 block.delta（provider 在同一 model call 内于 on_tool_start 之后
  // 继续吐正文）会把 active_state 从 tool_call/tool_execution 拉回 model_output，
  // 而同一时点的后端快照仍是 tool_call/tool_execution，两条链路给出不同 UI。
  const operation = stringValue(payload.operation) ?? "append";
  if (operation === "append" && typeof payload.text === "string") {
    block.text = boundedMessageStreamText(
      block.text + payload.text,
      MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS,
    );
  }
  if (operation === "redacted" || payload.redacted === true) block.redacted = true;
  if (operation === "item_upsert" || operation === "item_patch") {
    const item = isRecord(payload.item) ? payload.item : null;
    if (!item) return;
    const itemId = stringValue(item.id);
    const index = itemId
      ? block.items.findIndex((existing) => existing.id === itemId)
      : -1;
    if (index === -1) block.items.push({ ...item });
    else block.items[index] = operation === "item_patch"
      ? { ...block.items[index], ...item }
      : { ...item };
  }
}

function finishRunningBlocks(
  state: MessageStreamState,
  status: "failed" | "interrupted",
  event: MessageStreamEvent,
): void {
  for (const block of state.blocks) {
    if (block.status === "running") {
      block.status = status;
      block.completion_reason = status === "interrupted" ? "user_interrupt" : "execution_lost";
      block.partial = true;
      applyLifecycle(block, event, true);
    }
  }
}

function failureFromPayload(payload: Record<string, unknown>): MessageStreamState["failure"] {
  // 与快照 hydration 共用唯一归一实现，从结构上杜绝两条链路再次分叉。
  return failureFromValue(payload);
}

// 与后端 _set_terminal_active_state 一致：reason 依次取 completion_reason、code，最后落到 status。
function terminalReason(payload: Record<string, unknown>, status: string): string {
  return stringValue(payload.completion_reason) ?? stringValue(payload.code) ?? status;
}

function isTerminalEvent(type: MessageStreamEventType): boolean {
  return type === "stream.completed" || type === "stream.interrupted" || type === "stream.failed";
}
