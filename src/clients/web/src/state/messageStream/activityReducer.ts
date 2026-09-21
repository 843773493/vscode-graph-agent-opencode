// 活动与模型调用归约：维护 activity / model_call 两类"进行中投影"的 upsert 与终态收口。
// 由 eventReducer 的事件 switch 调用；activeStateAfter 与 modelOutputPhase 同时被 block、tool 链路复用。
import {
  applyLifecycle,
  booleanValue,
  isRecord,
  lifecycleFromValue,
  stringValue,
} from "./state";
import type {
  MessageStreamActiveState,
  MessageStreamActivity,
  MessageStreamEvent,
  MessageStreamState,
} from "./types";

export function activityStatusValue(value: unknown): MessageStreamActivity["status"] {
  return value === "running"
    || value === "waiting"
    || value === "stopping"
    || value === "completed"
    || value === "failed"
    || value === "unknown"
    ? value
    : "unknown";
}

export function modelOutputPhase(carrierType: string): string {
  return [
    "reasoning",
    "reasoning_content",
    "reasoning_items",
    "thinking",
    "redacted_thinking",
  ].includes(carrierType) ? "reasoning" : "text";
}

export function activeStateAfter(
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

export function activityFromPayload(payload: Record<string, unknown>): MessageStreamActivity | null {
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
    activity.completion_reason = reason;
    if (activity.status === "failed") activity.resumable = false;
    applyLifecycle(activity, event, true);
  }
}
