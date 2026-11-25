// 工具事件归约：ToolCall 参数累积、ToolExecution upsert 与执行终态收口。
// 由 eventReducer 的事件 switch 调用，保持纯函数、不持有独立状态。
import {
  MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS,
  applyLifecycle,
  boundedMessageStreamText,
  defaultedTextValue,
  isRecord,
  lifecycleFromValue,
  optionalTextValue,
  stringValue,
  toolExecutionOutcomeValue,
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
  if (!executionId) throw new Error("工具执行事件缺少 tool_execution_id");
  const existingExecution = state.toolExecutions.find(
    (tool) => tool.tool_execution_id === executionId,
  );
  const execution = { ...existingExecution, ...toolFromPayload(payload, status, executionId) };
  applyLifecycle(execution, event, status !== "running");
  // 工具开始是源端观察；信封时间只记录镜像发布，不补造执行边界。
  execution.started_at = stringValue(payload.started_at) ?? existingExecution?.started_at;
  state.toolExecutions = existingExecution
    ? state.toolExecutions.map((item) => item === existingExecution ? execution : item)
    : [...state.toolExecutions, execution];
}

export function withToolIdentityFallback(
  payload: Record<string, unknown>,
  event: MessageStreamEvent,
): Record<string, unknown> {
  const identityFields = [
    "model_call_id",
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
  state.toolExecutions = state.toolExecutions.map((execution) => {
    if (execution.status !== "running") return execution;
    const completed: MessageStreamToolExecution = { ...execution, status: "completed",
      outcome: "outcome_unknown", completion_reason: "execution_lost" };
    applyLifecycle(completed, event, true);
    return completed;
  });
}

export function finishRunningToolCalls(
  state: MessageStreamState,
  event: MessageStreamEvent,
  reason: "user_interrupt" | "execution_lost",
): void {
  state.toolCalls = Object.fromEntries(Object.entries(state.toolCalls).map(([id, toolCall]) => {
    const status = stringValue(toolCall.status);
    if (status !== "accumulating" && status !== "streaming" && status !== "running") return [id, toolCall];
    const completed = { ...toolCall,
      status: reason === "user_interrupt" && toolCall.arguments_complete === true ? "cancelled" : "incomplete",
      completion_reason: reason };
    applyLifecycle(completed, event, true);
    return [id, completed];
  }));
}

function toolFromPayload(
  payload: Record<string, unknown>,
  status: MessageStreamToolExecution["status"],
  executionId: string,
): MessageStreamToolExecution {
  const toolCallId = stringValue(payload.tool_call_id);
  if (!toolCallId) throw new Error("工具执行事件缺少 tool_call_id");
  return {
    tool_execution_id: executionId,
    tool_call_id: toolCallId,
    tool_invocation_id: optionalTextValue(payload.tool_invocation_id),
    tool_attempt_id: optionalTextValue(payload.tool_attempt_id),
    tool_name: defaultedTextValue(payload.tool_name, "tool"),
    status,
    outcome: toolExecutionOutcomeValue(payload.outcome),
    execution_completed_at: optionalTextValue(payload.execution_completed_at),
    completion_reason: optionalTextValue(payload.completion_reason),
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
  const { arguments_delta: argumentsDelta, ...fields } = incoming;
  const merged = { ...previous, ...fields };
  if (typeof argumentsDelta === "string" && incoming.arguments === undefined) {
    const raw = isRecord(previous?.arguments) && typeof previous.arguments.raw === "string"
      ? previous.arguments.raw : "";
    merged.arguments = { raw: raw + argumentsDelta };
  }
  if (previous && stringValue(previous.tool_name) && !stringValue(incoming.tool_name)) {
    merged.tool_name = previous.tool_name;
  }
  if (previous && incoming.arguments_complete !== true
    && hasArguments(previous.arguments) && !hasArguments(merged.arguments)) {
    merged.arguments = previous.arguments;
  }
  return merged;
}

function hasArguments(value: unknown): boolean {
  if (typeof value === "string") return value.length > 0;
  return isRecord(value) ? Object.keys(value).length > 0 : value != null;
}
