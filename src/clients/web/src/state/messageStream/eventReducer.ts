// SSE 数据事件 reducer：按 event_seq 推进消息流状态，维护实体 upsert 与终态收口。
// 与 snapshotHydration 互为递归：乱序事件需要快照兜底，快照恢复后需要重放缓冲事件。
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
  findBlock,
  isRecord,
  isTerminalStatus,
  lifecycleFromValue,
  numberValue,
  stringValue,
} from "./state";
import {
  finishRunningToolCalls,
  markRunningToolsUnknown,
  mergeToolCall,
  toolExecutionStatus,
  upsertTool,
  withToolIdentityFallback,
} from "./toolReducer";
import type {
  MessageStreamActiveState,
  MessageStreamActivity,
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
  state.connectionStatus = isTerminalEvent(event.type) ? "terminal" : "connected";
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
      state.activeState = activeStateAfter(state.activeState, "model_output", "validating", "completed");
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
        block.completion_reason = stringValue(payload.completion_reason) ?? "upstream_completed";
        block.partial = booleanValue(payload.partial) ?? false;
        applyLifecycle(block, event, true);
        state.activeState = activeStateAfter(
          state.activeState,
          "model_output",
          "completed",
          block.status,
          block.block_id,
        );
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
        state.activeState = {
          kind: "tool_call",
          phase: "arguments",
          entity_id: toolCallId,
          tool_call_id: toolCallId,
          tool_invocation_id: stringValue(toolCallPayload.tool_invocation_id) ?? undefined,
          tool_attempt_id: stringValue(toolCallPayload.tool_attempt_id) ?? undefined,
          status: "running",
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
        state.activeState = activeStateAfter(
          state.activeState,
          "tool_call",
          "completed",
          stringValue(payload.status) ?? "completed",
          toolCallId,
          toolCallPayload,
        );
      }
      break;
    }
    case "tool.started":
      upsertTool(
        state,
        withToolIdentityFallback(payload, event),
        "running",
        event,
      );
      state.agentLoopStatus = "tool_running";
      state.activeState = {
        kind: "tool_execution",
        phase: "running",
        entity_id: stringValue(payload.tool_execution_id) ?? event.tool_execution_id ?? "",
        tool_call_id: stringValue(payload.tool_call_id) ?? event.tool_call_id ?? undefined,
        tool_invocation_id: stringValue(payload.tool_invocation_id) ?? event.tool_invocation_id ?? undefined,
        tool_attempt_id: stringValue(payload.tool_attempt_id) ?? event.tool_attempt_id ?? undefined,
        tool_execution_id: stringValue(payload.tool_execution_id) ?? event.tool_execution_id ?? undefined,
        status: "running",
      };
      break;
    case "tool.completed":
      upsertTool(
        state,
        withToolIdentityFallback(payload, event),
        toolExecutionStatus(payload.status),
        event,
      );
      state.activeState = activeStateAfter(
        state.activeState,
        "tool_execution",
        "completed",
        toolExecutionStatus(payload.status),
        stringValue(payload.tool_execution_id) ?? event.tool_execution_id ?? undefined,
        withToolIdentityFallback(payload, event),
      );
      break;
    case "activity.started":
    case "activity.updated":
    case "activity.completed":
    case "activity.failed":
      upsertActivity(state, payload, event);
      break;
    case "interrupt.requested":
      state.streamStatus = "interrupting";
      state.activeState = {
        kind: "interrupt",
        phase: "requested",
        entity_id: stringValue(payload.interrupt_request_id) ?? "",
        status: "requested",
        reason: stringValue(payload.reason) ?? undefined,
      };
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "requested",
        reason: stringValue(payload.reason) ?? undefined,
        factConfirmed: false,
      };
      break;
    case "interrupt.rejected":
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "rejected",
        reason: stringValue(payload.reason) ?? undefined,
        factConfirmed: false,
      };
      break;
    case "stream.completed":
      state.streamStatus = "completed";
      state.agentLoopStatus = "completed";
      state.resumable = false;
      state.activeState = activeStateAfter(state.activeState, "stream", "completed", "completed");
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
      state.activeState = activeStateAfter(state.activeState, "stream", "interrupted", "interrupted");
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
      state.activeState = activeStateAfter(state.activeState, "stream", "failed", "failed");
      break;
  }
  return drainPendingEvents(state);
}

export function drainPendingEvents(state: MessageStreamState): MessageStreamState {
  if (state.pendingEvents.length === 0) return state;
  let next: MessageStreamState = { ...state, pendingEvents: [] };
  for (const pending of [...state.pendingEvents].sort((left, right) => left.event_seq - right.event_seq)) {
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
    carrier_type: stringValue(payload.carrier_type) ?? "text",
    status,
    text: "",
    items: [],
    redacted: booleanValue(payload.redacted) ?? false,
    projection: stringValue(payload.projection) ?? "streaming",
    completion_reason: stringValue(payload.completion_reason) ?? undefined,
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
  state.activeState = {
    kind: "model_output",
    phase: modelOutputPhase(block.carrier_type),
    entity_id: block.block_id,
    block_id: block.block_id,
    carrier_type: block.carrier_type,
    status: "running",
  };
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

function finishRunningModelCalls(
  state: MessageStreamState,
  event: MessageStreamEvent,
  outcome: "user_interrupt" | "execution_lost",
): void {
  for (const modelCall of Object.values(state.modelCalls)) {
    if (stringValue(modelCall.status) !== "running") continue;
    modelCall.status = "failed";
    modelCall.outcome = outcome;
    modelCall.retryable = false;
    modelCall.completion_reason = outcome;
    applyLifecycle(modelCall, event, true);
  }
}

function finishRunningActivities(
  state: MessageStreamState,
  event: MessageStreamEvent,
  reason: "user_interrupt" | "execution_lost",
): void {
  for (const activity of state.activities) {
    if (activity.status !== "running" && activity.status !== "waiting" && activity.status !== "stopping") continue;
    const externallySideEffecting = !["none", "read_only"].includes(activity.side_effect_policy);
    const uncertainInterrupt = reason === "user_interrupt" && externallySideEffecting;
    activity.status = reason === "execution_lost" || uncertainInterrupt ? "failed" : "completed";
    activity.outcome = reason === "execution_lost"
      ? "execution_lost"
      : uncertainInterrupt ? "outcome_unknown" : reason;
    activity.completion_reason = reason;
    if (activity.status === "failed") activity.resumable = false;
    applyLifecycle(activity, event, true);
  }
}

function failureFromPayload(payload: Record<string, unknown>): MessageStreamState["failure"] {
  return failureFromUnknown(payload);
}

function failureFromUnknown(value: unknown): MessageStreamState["failure"] {
  if (!isRecord(value)) return null;
  const message = stringValue(value.message);
  if (!message) return null;
  return {
    code: stringValue(value.code) ?? "message_stream_failure",
    message,
    afterInterruptRequested: booleanValue(value.after_interrupt_requested) ?? false,
    resumable: booleanValue(value.resumable) ?? false,
  };
}

function isTerminalEvent(type: MessageStreamEventType): boolean {
  return type === "stream.completed" || type === "stream.interrupted" || type === "stream.failed";
}

function activityStatusValue(value: unknown): MessageStreamActivity["status"] {
  return value === "running"
    || value === "waiting"
    || value === "stopping"
    || value === "completed"
    || value === "failed"
    || value === "unknown"
    ? value
    : "unknown";
}

function modelOutputPhase(carrierType: string): string {
  return [
    "reasoning",
    "reasoning_content",
    "reasoning_items",
    "thinking",
    "redacted_thinking",
  ].includes(carrierType) ? "reasoning" : "text";
}

function activeStateAfter(
  previous: MessageStreamActiveState | null,
  kind: string,
  phase: string,
  status: string,
  entityId?: string,
  identityPayload?: Record<string, unknown>,
): MessageStreamActiveState {
  const toolCallId = stringValue(identityPayload?.tool_call_id);
  const toolInvocationId = stringValue(identityPayload?.tool_invocation_id);
  const toolAttemptId = stringValue(identityPayload?.tool_attempt_id);
  const toolExecutionId = stringValue(identityPayload?.tool_execution_id);
  return {
    kind,
    phase,
    entity_id: entityId ?? previous?.entity_id ?? "",
    carrier_type: previous?.carrier_type,
    block_id: previous?.block_id,
    tool_call_id: toolCallId ?? previous?.tool_call_id,
    tool_invocation_id: toolInvocationId ?? previous?.tool_invocation_id,
    tool_attempt_id: toolAttemptId ?? previous?.tool_attempt_id,
    tool_execution_id: toolExecutionId ?? previous?.tool_execution_id,
    activity_id: previous?.activity_id,
    activity_kind: previous?.activity_kind,
    status,
    last_kind: previous?.kind,
    last_phase: previous?.phase,
    reason: previous?.reason,
    detail_ref: previous?.detail_ref,
  };
}

function activityFromPayload(payload: Record<string, unknown>): MessageStreamActivity | null {
  const activityId = stringValue(payload.activity_id);
  const kind = stringValue(payload.kind);
  if (!activityId || !kind) return null;
  return {
    activity_id: activityId,
    kind,
    parent_activity_id: stringValue(payload.parent_activity_id) ?? undefined,
    scope_ref: stringValue(payload.scope_ref) ?? "turn",
    status: activityStatusValue(payload.status),
    outcome: stringValue(payload.outcome) ?? undefined,
    summary: stringValue(payload.summary) ?? undefined,
    cancellable: booleanValue(payload.cancellable) ?? false,
    resumable: booleanValue(payload.resumable) ?? false,
    side_effect_policy: stringValue(payload.side_effect_policy) ?? "unknown",
    resource_refs: Array.isArray(payload.resource_refs)
      ? payload.resource_refs.filter((item): item is string => typeof item === "string")
      : [],
    detail: isRecord(payload.detail) ? { ...payload.detail } : undefined,
    detail_ref: stringValue(payload.detail_ref) ?? undefined,
    detail_available: booleanValue(payload.detail_available) ?? false,
    detail_error: stringValue(payload.detail_error) ?? undefined,
    completion_reason: stringValue(payload.completion_reason) ?? undefined,
    ...lifecycleFromValue(payload),
  };
}

function upsertActivity(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  event: MessageStreamEvent,
): void {
  const activity = activityFromPayload(payload);
  if (!activity) return;
  const existing = state.activities.find((item) => item.activity_id === activity.activity_id);
  if (existing) {
    Object.assign(existing, activity);
    applyLifecycle(existing, event, event.type === "activity.completed" || event.type === "activity.failed");
  } else {
    applyLifecycle(activity, event, event.type === "activity.completed" || event.type === "activity.failed");
    state.activities.push(activity);
  }
  state.activeState = {
    kind: "activity",
    phase: activity.status,
    entity_id: activity.activity_id,
    activity_id: activity.activity_id,
    activity_kind: activity.kind,
    status: activity.status,
    detail_ref: activity.detail_ref,
  };
}

function upsertModelCall(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  status: string,
  event: MessageStreamEvent,
): void {
  const id = stringValue(payload.model_call_id);
  if (!id) return;
  state.modelCalls[id] = {
    ...state.modelCalls[id],
    ...payload,
    status,
  };
  applyLifecycle(
    state.modelCalls[id],
    event,
    status === "completed" || status === "failed",
  );
}
