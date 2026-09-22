// 消息流展示投影：把消息流状态折叠成 TurnResponsePart 响应片段。
import type { TurnResponsePart } from "../../types/backend";
import {
  compareLifecycleEntities,
  isRecord,
  sortBlocks,
  sortToolExecutions,
  sortedToolCallEntries,
  stringValue,
} from "./state";
import type {
  MessageStreamBlock,
  MessageStreamState,
  MessageStreamToolExecution,
} from "./types";

type MessageStreamResponseEntity =
  | {
    kind: "block";
    value: MessageStreamBlock;
    fallback: number;
    id: string;
  }
  | {
    kind: "tool_execution";
    value: MessageStreamToolExecution;
    fallback: number;
    id: string;
  }
  | {
    kind: "tool_call";
    value: Record<string, unknown>;
    fallback: number;
    id: string;
  };

/**
 * 后端在请求边界 ToolMessage 先完成 canonical 结果、外层事件流的
 * on_tool_start/on_tool_end 迟到时，会为同一 tool_call_id 写第二条
 * execution（completion_reason=reconciled_tool_message 的生命周期收口事件，
 * 见后端 complete_tool 的 reconciled 契约：只闭合生命周期，不是第二次
 * 工具执行）。投影必须按 tool_call_id 折叠成一个逻辑工具，否则同一工具
 * 会在聊天时间线中渲染两次。
 */
function projectableToolExecutions(
  executions: readonly MessageStreamToolExecution[],
): MessageStreamToolExecution[] {
  const selectedByCallId = new Map<string, MessageStreamToolExecution>();
  for (const execution of sortToolExecutions(executions)) {
    const current = selectedByCallId.get(execution.tool_call_id);
    if (current === undefined || toolExecutionRank(execution) < toolExecutionRank(current)) {
      selectedByCallId.set(execution.tool_call_id, execution);
    }
  }
  return [...selectedByCallId.values()];
}

/** 权威 execution 优先：非 reconciled 终态 > 非 reconciled running > reconciled 收口。 */
function toolExecutionRank(execution: MessageStreamToolExecution): number {
  if (execution.completion_reason === "reconciled_tool_message") return 2;
  return execution.status === "running" ? 1 : 0;
}

/**
 * 展示层的工具状态，语义与 state 层 `toolExecutionStatusValue` 有意不同：
 * state 层把 outcome_unknown/provider_error 收敛为 `completed`，以便与后端
 * tool.completed 的归一结果保持一致（那是权威状态事实，不得改）；而展示层
 * 要告诉用户"结果未知/提供方出错，不能当作成功"，故降为 `failed`。
 * 二者不是同一原语，禁止合并趋同。
 */
function toolExecutionDisplayStatus(
  execution: MessageStreamToolExecution,
): "running" | "completed" | "failed" {
  if (execution.status === "failed") return "failed";
  if (execution.outcome === "outcome_unknown" || execution.outcome === "provider_error") {
    return "failed";
  }
  return execution.status === "completed" ? "completed" : "running";
}

export function messageStreamToResponseParts(
  state: MessageStreamState,
): TurnResponsePart[] {
  const parts: TurnResponsePart[] = [];
  const executionCallIds = new Set(
    state.toolExecutions.map((execution) => execution.tool_call_id),
  );
  const entities: MessageStreamResponseEntity[] = [
    ...sortBlocks(state.blocks).map((block) => ({
      kind: "block" as const,
      value: block,
      fallback: block.block_index,
      id: block.block_id,
    })),
    ...projectableToolExecutions(state.toolExecutions).map((execution) => ({
      kind: "tool_execution" as const,
      value: execution,
      fallback: 0,
      id: execution.tool_execution_id,
    })),
    ...sortedToolCallEntries(state.toolCalls)
      .filter(([toolCallId, toolCall]) => {
        if (executionCallIds.has(toolCallId)) return false;
        const callStatus = stringValue(toolCall.status);
        return callStatus === "incomplete"
          || callStatus === "cancelled"
          || toolCall.arguments_complete === false;
      })
      .map(([id, value]) => ({
        kind: "tool_call" as const,
        value,
        fallback: 0,
        id,
      })),
  ];
  entities.sort(compareLifecycleEntities);

  for (const entity of entities) {
    if (entity.kind === "block") {
      const block = entity.value;
      if (block.projection === "intermediate" || block.projection === "superseded") continue;
      const kind = block.redacted || block.carrier_type === "redacted_thinking"
        ? "reasoning_encrypted"
        : block.carrier_type === "text"
          ? "text"
          : block.carrier_type === "reasoning_items"
            ? "reasoning_summary"
            : "reasoning";
      const structuredText = block.items
        .map((item) => stringValue(item.text) ?? stringValue(item.content) ?? stringValue(item.summary) ?? "")
        .filter(Boolean)
        .join("\n");
      const text = block.redacted ? "" : block.text || structuredText;
      if (text || block.redacted || block.items.length > 0) {
        parts.push({
          part_id: block.block_id,
          kind,
          projection: "streaming",
          status: block.status === "completed" || block.status === "running"
            ? block.status
            : "failed",
          source: {
            message_sequence: 0,
            content_block_index: block.block_index,
          },
          text,
          carrier_type: block.carrier_type,
          completion_reason: block.completion_reason,
          partial: block.partial ?? false,
          final: block.status === "completed" && !block.partial,
        });
      }
      continue;
    }
    if (entity.kind === "tool_execution") {
      const execution = entity.value;
      const toolCall = state.toolCalls[execution.tool_call_id];
      const argumentsValue = toolCall?.arguments;
      const status = toolExecutionDisplayStatus(execution);
      const outcomeUnknown = execution.outcome === "outcome_unknown";
      parts.push({
        part_id: execution.tool_execution_id,
        kind: "tool_call",
        projection: "streaming",
        status,
        source: { message_sequence: 0 },
        text: "",
        tool_call_id: execution.tool_call_id,
        tool_name: execution.tool_name,
        arguments: typeof argumentsValue === "string"
          ? argumentsValue
          : argumentsValue ? JSON.stringify(argumentsValue) : "",
        outcome_unknown: outcomeUnknown,
        completion_reason: execution.completion_reason,
        final: status === "completed",
      });
      if (
        execution.result
        || execution.error
        || execution.status === "completed"
        || execution.status === "failed"
      ) {
        parts.push({
          part_id: `${execution.tool_execution_id}:result`,
          kind: "tool_result",
          projection: "streaming",
          status,
          source: { message_sequence: 0 },
          text: execution.result ?? execution.error ?? "",
          result: execution.result ?? execution.error ?? "",
          tool_call_id: execution.tool_call_id,
          tool_name: execution.tool_name,
          outcome_unknown: outcomeUnknown,
          completion_reason: execution.completion_reason,
          final: status === "completed",
        });
      }
      continue;
    }
    const toolCallId = entity.id;
    const toolCall = entity.value;
    const callStatus = stringValue(toolCall.status);
    const argumentsValue = toolCall.arguments;
    parts.push({
      part_id: toolCallId,
      kind: "tool_call",
      projection: "streaming",
      status: callStatus === "cancelled" ? "cancelled" : "failed",
      source: { message_sequence: 0 },
      text: "",
      tool_call_id: toolCallId,
      tool_name: stringValue(toolCall.tool_name) ?? "tool",
      arguments: typeof argumentsValue === "string"
        ? argumentsValue
        : isRecord(argumentsValue) ? JSON.stringify(argumentsValue) : "",
      outcome_unknown: false,
      final: true,
    });
  }
  return parts;
}
