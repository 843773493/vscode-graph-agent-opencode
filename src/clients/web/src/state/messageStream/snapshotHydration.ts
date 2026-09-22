// 快照 hydration：把后端权威 snapshot 反序列化为完整消息流状态，并重放已缓冲的乱序事件。
import type {
  MessageStreamSnapshot,
  MessageStreamSnapshotResponse,
} from "../../api/messageStreamSnapshot";
import { drainPendingEvents } from "./eventReducer";
import {
  MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS,
  MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS,
  activityStatusValue,
  blockStatusValue,
  boundedMessageStreamText,
  cloneMessageStreamState,
  createMessageStreamState,
  defaultedTextValue,
  failureFromValue,
  isTerminalStatus,
  optionalTextValue,
  sortActivities,
  sortBlocks,
  sortToolExecutions,
  toolExecutionOutcomeValue,
  toolExecutionStatusValue,
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
  next.blocks = sortBlocks(
    snapshot.blocks.map(blockFromSnapshot),
  );
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
      reason: optionalTextValue(snapshot.interrupt_state.reason),
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
    model_call_id: blockModelCallId(value),
    block_index: value.block_index ?? 0,
    carrier_type: defaultedTextValue(value.carrier_type, "text"),
    status: blockStatusValue(value.status),
    text: value.text
      ? boundedMessageStreamText(value.text, MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS)
      : "",
    items: value.items.map((item) => ({ ...item })),
    redacted: value.redacted ?? false,
    projection: defaultedTextValue(value.projection, "streaming"),
    completion_reason: blockCompletionReason(value),
    partial: value.partial ?? false,
    ...lifecycleFromSnapshot(value),
  };
}

/**
 * 快照 block 的 model_call_id 还原。
 *
 * 公共 MessageBlockSnapshot 不带该字段：codec 在快照归一化里把它作为内部对账
 * 字段摘除（app/protocol/codecs/message_stream.py 的 `block.pop("model_call_id")`），
 * 但事件路径的 upsertBlock 会从事件信封写入它，且 model.retrying 以
 * `block.model_call_id === state.currentModelCallId` 决定把哪些 block 的 projection
 * 置为 "intermediate"。快照恢复后该字段若恒为 null，同一 Turn 经快照恢复与经
 * 事件重放会得到不同的 projection，而 projection 决定 responseProjection 是否
 * 保留该 block 的文本（"intermediate" 会被丢弃），因此必须还原。
 *
 * 归属真源是 block_id 前缀：后端 StreamBlockAssemblyMixin._scoped_block_id 恒定
 * 构造 `${model_call_id or "unbound-model-call"}:block:${provider_block_id}`，与
 * block.started 事件信封写入的 model_call_id 同源恒等。事件序号无法承担该职责：
 * 同一段 model_calls 区间空隙里，带新 call 显式身份的 delta 归其后第一个 call，
 * 带旧 call 身份或空身份的 delta 归 current_model_call_id，两种情形 block 的
 * started_seq 形态完全相同，纯序号规则不可能同时正确。
 */
function blockModelCallId(value: SnapshotBlock): string | null {
  const marker = value.block_id.indexOf(":block:");
  // 后端 block_id 恒含 ':block:' 分隔符；缺失说明收到非法身份，不猜测归属。
  if (marker < 0) return null;
  const scoped = value.block_id.slice(0, marker);
  return scoped === "unbound-model-call" ? null : scoped;
}

/**
 * 快照 block 的 completion_reason 归一，必须与事件路径逐字一致：
 * 运行中 block 事件路径用 optionalTextValue（undefined），仅 block.completed 分支
 * 才兜底 "upstream_completed"；后端也只在 block.completed、stream.completed 自动收尾
 * 和 interrupted/failed 强制收尾时写入该字段，运行中快照必然缺失。
 * 终态兜底沿用 "upstream_completed"：interrupted/failed 收尾由后端无条件写入
 * "user_interrupt"/"execution_lost"，不会走到这里。
 * 判定必须基于 status 而非 projection：model.retrying 会把运行中 block 的
 * projection 置为 "intermediate"，据 projection 判定会伪造完成原因。
 */
function blockCompletionReason(value: SnapshotBlock): string | undefined {
  const reason = optionalTextValue(value.completion_reason);
  if (reason !== undefined) return reason;
  return blockStatusValue(value.status) === "running"
    ? undefined
    : "upstream_completed";
}

function toolFromSnapshot(value: SnapshotToolExecution): MessageStreamToolExecution {
  return {
    tool_execution_id: value.tool_execution_id,
    tool_call_id: value.tool_call_id,
    tool_invocation_id: optionalTextValue(value.tool_invocation_id),
    tool_attempt_id: optionalTextValue(value.tool_attempt_id),
    tool_name: defaultedTextValue(value.tool_name, "tool"),
    status: toolExecutionStatusValue(value.status),
    outcome: toolExecutionOutcomeValue(value.outcome),
    completion_reason: optionalTextValue(value.completion_reason),
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
    parent_activity_id: optionalTextValue(value.parent_activity_id),
    scope_ref: defaultedTextValue(value.scope_ref, "turn"),
    status: activityStatusValue(value.status),
    outcome: optionalTextValue(value.outcome),
    summary: optionalTextValue(value.summary),
    cancellable: value.cancellable ?? false,
    resumable: value.resumable ?? false,
    side_effect_policy: defaultedTextValue(value.side_effect_policy, "unknown"),
    resource_refs: [...value.resource_refs],
    detail: value.detail ? { ...value.detail } : undefined,
    detail_ref: optionalTextValue(value.detail_ref),
    detail_available: value.detail_available ?? false,
    detail_error: optionalTextValue(value.detail_error),
    ...lifecycleFromSnapshot(value),
  };
}

function failureFromSnapshot(
  value: MessageStreamSnapshot["failure"] | undefined,
): MessageStreamState["failure"] {
  // 必须与事件路径逐字段一致：message 是失败详情主体，缺失或空串都判定为无效
  // failure（事件路径返回 null）。proto3 string 无 presence，空 message 会在
  // SSE stream.snapshot 控制帧里被省略，透传会伪造 message=undefined 的假 failure。
  return failureFromValue(value);
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
