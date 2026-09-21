// 快照 hydration：把后端权威 snapshot 反序列化为完整消息流状态，并重放已缓冲的乱序事件。
import type {
  MessageStreamSnapshot,
  MessageStreamSnapshotResponse,
} from "../../api/messageStreamSnapshot";
import { drainPendingEvents } from "./eventReducer";
import {
  MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS,
  MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS,
  blockStatusValue,
  boundedMessageStreamText,
  cloneMessageStreamState,
  createMessageStreamState,
  isTerminalStatus,
  sortActivities,
  sortBlocks,
  sortToolExecutions,
} from "./state";
import type {
  MessageStreamActiveState,
  MessageStreamActivity,
  MessageStreamBlock,
  MessageStreamLifecycle,
  MessageStreamState,
  MessageStreamToolExecution,
} from "./types";

type SnapshotBlock = MessageStreamSnapshot["blocks"][number];

type SnapshotToolExecution = MessageStreamSnapshot["tool_executions"][number];

type SnapshotActivity = MessageStreamSnapshot["activities"][number];

type SnapshotActiveState = NonNullable<MessageStreamSnapshot["active_state"]>;

export function applyMessageStreamSnapshot(
  current: MessageStreamState,
  snapshot: MessageStreamSnapshotResponse,
): MessageStreamState {
  const state = cloneMessageStreamState(current);
  if (state.sessionId !== snapshot.session_id || state.turnId !== snapshot.turn_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流快照关联键与当前 Turn 不一致",
    };
  }
  if (state.workspaceId && snapshot.workspace_id && state.workspaceId !== snapshot.workspace_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流快照 workspace_id 与当前工作区不一致",
    };
  }
  if (state.turnStreamId && state.turnStreamId !== snapshot.turn_stream_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流快照 turn_stream_id 在同一 Turn 内发生变化",
    };
  }
  if (snapshot.snapshot_seq < state.lastEventSeq) return state;
  return drainPendingEvents(applySnapshotState(state, snapshot, snapshot));
}

type MessageStreamSnapshotIdentity = Pick<
  MessageStreamSnapshotResponse,
  "session_id" | "turn_id" | "turn_stream_id" | "workspace_id"
>;

export function applySnapshotState(
  current: MessageStreamState,
  snapshot: MessageStreamSnapshot,
  identity: MessageStreamSnapshotIdentity,
): MessageStreamState {
  const next = createMessageStreamState(current.sessionId, current.turnId, identity.turn_stream_id);
  next.workspaceId = snapshot.workspace_id ?? identity.workspace_id ?? current.workspaceId;
  next.lastEventSeq = snapshot.snapshot_seq;
  next.streamStatus = snapshot.stream_status;
  next.agentLoopStatus = snapshot.agent_loop_status;
  next.currentModelCallId = snapshot.current_model_call_id ?? null;
  next.currentAttempt = snapshot.current_attempt;
  next.blocks = sortBlocks(snapshot.blocks.map(blockFromSnapshot));
  next.toolCalls = toolCallsFromSnapshot(snapshot.tool_calls);
  next.toolExecutions = sortToolExecutions(snapshot.tool_executions.map(toolFromSnapshot));
  next.activeState = activeStateFromSnapshot(snapshot.active_state);
  next.activities = sortActivities(snapshot.activities.map(activityFromSnapshot));
  next.modelCalls = modelCallsFromSnapshot(snapshot.model_calls);
  next.resourceRefs = resourceRefsFromSnapshot(snapshot.resource_refs);
  next.recovery = snapshot.recovery ? { ...snapshot.recovery } : null;
  next.interruptState = snapshot.interrupt_state
    ? {
      requestId: snapshot.interrupt_state.request_id,
      status: snapshot.interrupt_state.status,
      reason: snapshot.interrupt_state.reason,
      factConfirmed: snapshot.interrupt_state.fact_confirmed,
    }
    : null;
  next.failure = failureFromSnapshot(snapshot.failure);
  next.resumable = snapshot.resumable;
  next.pendingEvents = current.pendingEvents.filter(
    (pending) => pending.event_seq > next.lastEventSeq,
  );
  const firstPendingSeq = next.pendingEvents[0]?.event_seq;
  const hasPendingGap = firstPendingSeq !== undefined
    && firstPendingSeq > next.lastEventSeq + 1;
  next.connectionStatus = isTerminalStatus(next.streamStatus)
    ? "terminal"
    : hasPendingGap ? "gap" : "connected";
  next.protocolError = hasPendingGap
    ? `消息流 event_seq 不连续: expected=${next.lastEventSeq + 1} actual=${firstPendingSeq}`
    : null;
  return next;
}

function lifecycleFromSnapshot(value: MessageStreamLifecycle): MessageStreamLifecycle {
  return {
    started_seq: value.started_seq,
    last_event_seq: value.last_event_seq,
    completed_seq: value.completed_seq,
    started_at: value.started_at,
    updated_at: value.updated_at,
    completed_at: value.completed_at,
  };
}

function blockFromSnapshot(value: SnapshotBlock): MessageStreamBlock {
  return {
    block_id: value.block_id,
    model_call_id: null,
    block_index: value.block_index ?? 0,
    carrier_type: value.carrier_type ?? "text",
    status: blockStatusValue(value.status),
    text: value.text
      ? boundedMessageStreamText(value.text, MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS)
      : "",
    items: value.items.map((item) => ({ ...item })),
    redacted: value.redacted ?? false,
    projection: value.projection ?? "streaming",
    completion_reason: value.completion_reason,
    partial: value.partial ?? false,
    ...lifecycleFromSnapshot(value),
  };
}

function toolFromSnapshot(value: SnapshotToolExecution): MessageStreamToolExecution {
  return {
    tool_execution_id: value.tool_execution_id,
    tool_call_id: value.tool_call_id,
    tool_invocation_id: value.tool_invocation_id,
    tool_attempt_id: value.tool_attempt_id,
    tool_name: value.tool_name,
    status: value.status,
    outcome: value.outcome,
    completion_reason: value.completion_reason,
    result: value.result
      ? boundedMessageStreamText(value.result, MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS)
      : value.result,
    error: value.error
      ? boundedMessageStreamText(value.error, MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS)
      : value.error,
    ...lifecycleFromSnapshot(value),
  };
}

function toolCallsFromSnapshot(value: MessageStreamSnapshot["tool_calls"]): Record<string, Record<string, unknown>> {
  const calls: Record<string, Record<string, unknown>> = {};
  for (const item of value) calls[item.tool_call_id] = { ...item };
  return calls;
}

function activeStateFromSnapshot(value: SnapshotActiveState | undefined): MessageStreamActiveState | null {
  if (!value) return null;
  return {
    kind: value.kind,
    phase: value.phase,
    entity_id: value.entity_id,
    carrier_type: value.carrier_type,
    block_id: value.block_id,
    tool_call_id: value.tool_call_id,
    tool_execution_id: value.tool_execution_id,
    tool_invocation_id: value.tool_invocation_id,
    tool_attempt_id: value.tool_attempt_id,
    activity_id: value.activity_id,
    activity_kind: value.activity_kind,
    status: value.status,
    last_kind: value.last_kind,
    last_phase: value.last_phase,
    reason: value.reason,
    detail_ref: value.detail_ref,
  };
}

function activityFromSnapshot(value: SnapshotActivity): MessageStreamActivity {
  return {
    activity_id: value.activity_id,
    kind: value.kind,
    parent_activity_id: value.parent_activity_id,
    scope_ref: value.scope_ref ?? "turn",
    status: value.status,
    outcome: value.outcome,
    summary: value.summary,
    cancellable: value.cancellable ?? false,
    resumable: value.resumable ?? false,
    side_effect_policy: value.side_effect_policy ?? "unknown",
    resource_refs: [...value.resource_refs],
    detail: value.detail ? { ...value.detail } : undefined,
    detail_ref: value.detail_ref,
    detail_available: value.detail_available ?? false,
    detail_error: value.detail_error,
    ...lifecycleFromSnapshot(value),
  };
}

function failureFromSnapshot(
  value: MessageStreamSnapshot["failure"] | undefined,
): MessageStreamState["failure"] {
  if (!value) return null;
  return {
    code: value.code,
    message: value.message,
    afterInterruptRequested: value.after_interrupt_requested ?? false,
    resumable: value.resumable ?? false,
  };
}

function modelCallsFromSnapshot(value: MessageStreamSnapshot["model_calls"]): Record<string, Record<string, unknown>> {
  const calls: Record<string, Record<string, unknown>> = {};
  for (const item of value) calls[item.model_call_id] = { ...item };
  return calls;
}

function resourceRefsFromSnapshot(value: MessageStreamSnapshot["resource_refs"]): Record<string, Record<string, unknown>> {
  const refs: Record<string, Record<string, unknown>> = {};
  for (const item of value) refs[item.resource_id] = { ...item };
  return refs;
}
