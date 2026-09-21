// 工具事件归约：ToolCall 参数累积、ToolExecution upsert 与执行终态收口。
// 由 eventReducer 的事件 switch 调用，保持纯函数、不持有独立状态。
import {
  MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS,
  applyLifecycle,
  boundedMessageStreamText,
  isRecord,
  lifecycleFromValue,
  stringValue,
} from "./state";
import type {
  MessageStreamEvent,
  MessageStreamState,
  MessageStreamToolExecution,
} from "./types";

export function upsertTool(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  status: MessageStreamToolExecution["status"],
  event: MessageStreamEvent,
): void {
  const executionId = stringValue(payload.tool_execution_id);
  if (!executionId) return;
  const existing = state.toolExecutions.find((tool) => tool.tool_execution_id === executionId);
  if (existing) {
    Object.assign(existing, toolFromPayload(payload, status));
    applyLifecycle(existing, event, status !== "running");
    return;
  }
  const execution = toolFromPayload(payload, status);
  applyLifecycle(execution, event, status !== "running");
  state.toolExecutions.push(execution);
}

export function withToolIdentityFallback(
  payload: Record<string, unknown>,
  event: MessageStreamEvent,
): Record<string, unknown> {
  const identityFields = [
    "tool_execution_id",
    "tool_call_id",
    "tool_invocation_id",
    "tool_attempt_id",
  ] as const;
  const fallback = { ...payload };
  for (const field of identityFields) {
    if (!fallback[field] && event[field]) fallback[field] = event[field];
  }
  return fallback;
}

export function markRunningToolsUnknown(
  state: MessageStreamState,
  event: MessageStreamEvent,
): void {
  for (const execution of state.toolExecutions) {
    if (execution.status === "running") {
      execution.status = "completed";
      execution.outcome = "outcome_unknown";
      execution.completion_reason = "execution_lost";
      applyLifecycle(execution, event, true);
    }
  }
}

export function finishRunningToolCalls(
  state: MessageStreamState,
  event: MessageStreamEvent,
  reason: "user_interrupt" | "execution_lost",
): void {
  for (const toolCall of Object.values(state.toolCalls)) {
    const status = stringValue(toolCall.status);
    if (status !== "accumulating" && status !== "streaming" && status !== "running") continue;
    toolCall.status = reason === "user_interrupt" && toolCall.arguments_complete === true
      ? "cancelled"
      : "incomplete";
    toolCall.completion_reason = reason;
    applyLifecycle(toolCall, event, true);
  }
}

function toolFromPayload(
  payload: Record<string, unknown>,
  status: MessageStreamToolExecution["status"],
): MessageStreamToolExecution {
  return {
    tool_execution_id: stringValue(payload.tool_execution_id) ?? "unknown-tool-execution",
    tool_call_id: stringValue(payload.tool_call_id) ?? "unknown-tool-call",
    tool_invocation_id: stringValue(payload.tool_invocation_id) ?? undefined,
    tool_attempt_id: stringValue(payload.tool_attempt_id) ?? undefined,
    tool_name: stringValue(payload.tool_name) ?? "tool",
    status,
    outcome: toolExecutionOutcome(payload.outcome),
    completion_reason: stringValue(payload.completion_reason) ?? undefined,
    result: typeof payload.result === "string"
      ? boundedMessageStreamText(payload.result, MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS)
      : undefined,
    error: typeof payload.error === "string"
      ? boundedMessageStreamText(payload.error, MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS)
      : undefined,
    ...lifecycleFromValue(payload),
  };
}

export function mergeToolCall(
  previous: Record<string, unknown> | undefined,
  incoming: Record<string, unknown>,
): Record<string, unknown> {
  if (!previous) return { ...incoming };
  const merged = { ...previous, ...incoming };
  if (stringValue(previous.tool_name) && !stringValue(incoming.tool_name)) {
    merged.tool_name = previous.tool_name;
  }
  if (hasArguments(previous.arguments) && !hasArguments(incoming.arguments)) {
    merged.arguments = previous.arguments;
  }
  return merged;
}

function hasArguments(value: unknown): boolean {
  if (typeof value === "string") return value.length > 0;
  return isRecord(value) ? Object.keys(value).length > 0 : value != null;
}

export function toolExecutionStatus(value: unknown): MessageStreamToolExecution["status"] {
  if (value === "failed") return "failed";
  if (value === "completed" || value === "succeeded" || value === "outcome_unknown") {
    return "completed";
  }
  return "running";
}

function toolExecutionOutcome(value: unknown): MessageStreamToolExecution["outcome"] {
  return value === "success"
    || value === "provider_error"
    || value === "execution_lost"
    || value === "outcome_unknown"
    ? value
    : undefined;
}
