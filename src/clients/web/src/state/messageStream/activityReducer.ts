// 活动与模型调用归约：维护 activity / model_call 两类"进行中投影"的 upsert 与终态收口。
// 由 eventReducer 的事件 switch 调用；中断/终态 activeState 与 modelOutputPhase 同时被 block、tool 链路复用。
import {
  activityStatusValue,
  applyLifecycle,
  booleanValue,
  defaultedTextValue,
  isRecord,
  lifecycleFromValue,
  optionalTextValue,
  stringValue,
} from "./state";
import type {
  MessageStreamActiveState,
  MessageStreamActivity,
  MessageStreamEvent,
  MessageStreamState,
} from "./types";

export function modelOutputPhase(carrierType: string): string {
  return [
    "reasoning",
    "reasoning_content",
    "reasoning_items",
    "thinking",
    "redacted_thinking",
  ].includes(carrierType) ? "reasoning" : "text";
}

/**
 * 中断进行中的 active_state：逐字段对齐后端 interrupt.requested 分支
 * （message_stream_store.py `_apply_event`）。取值必须是 interrupting/stopping，
 * 并且只保留上一状态作为 last_kind/last_phase，不得携带 block/tool/activity 归属字段。
 */
export function interruptingActiveState(
  previous: MessageStreamActiveState | null,
  entityId: string,
  reason: string | undefined,
): MessageStreamActiveState {
  return {
    kind: "interrupting",
    phase: "stopping",
    entity_id: entityId,
    status: "stopping",
    last_kind: previous?.kind,
    last_phase: previous?.phase,
    reason,
  };
}

/**
 * 消息流终态的 active_state：逐字段对齐后端 `_set_terminal_active_state`。
 * kind 固定为 terminal、phase 等于 status，并附上一状态作为 last_kind/last_phase。
 */
export function terminalActiveState(
  previous: MessageStreamActiveState | null,
  entityId: string,
  status: string,
  reason: string | undefined,
): MessageStreamActiveState {
  return {
    kind: "terminal",
    phase: status,
    entity_id: entityId,
    status,
    last_kind: previous?.kind,
    last_phase: previous?.phase,
    reason,
  };
}

function activityFromPayload(payload: Record<string, unknown>): MessageStreamActivity | null {
  const activityId = stringValue(payload.activity_id);
  const kind = stringValue(payload.kind);
  if (!activityId || !kind) return null;
  return {
    activity_id: activityId,
    kind,
    parent_activity_id: optionalTextValue(payload.parent_activity_id),
    scope_ref: defaultedTextValue(payload.scope_ref, "turn"),
    status: activityStatusValue(payload.status),
    outcome: optionalTextValue(payload.outcome),
    summary: optionalTextValue(payload.summary),
    cancellable: booleanValue(payload.cancellable) ?? false,
    resumable: booleanValue(payload.resumable) ?? false,
    side_effect_policy: defaultedTextValue(payload.side_effect_policy, "unknown"),
    resource_refs: Array.isArray(payload.resource_refs)
      ? payload.resource_refs.filter((item): item is string => typeof item === "string")
      : [],
    detail: isRecord(payload.detail) ? { ...payload.detail } : undefined,
    detail_ref: optionalTextValue(payload.detail_ref),
    detail_available: booleanValue(payload.detail_available) ?? false,
    detail_error: optionalTextValue(payload.detail_error),
    // TODO: Activity 的 completion_reason 属于后端内部诊断字段，公共
    // message.v1 未声明该字段，codec 会在事件投影与快照投影中一律摘除
    // （app/protocol/codecs/message_stream.py 的 activity.* 与 snapshot
    // activities 两处）。事件侧既无法收到该字段，也不得自行发明，故不再读写。
    ...lifecycleFromValue(payload),
  };
}

export function upsertActivity(
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

export function upsertModelCall(
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

export function finishRunningModelCalls(
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

export function finishRunningActivities(
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
    if (activity.status === "failed") activity.resumable = false;
    applyLifecycle(activity, event, true);
  }
}
