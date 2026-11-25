// 消息流展示投影：把消息流状态折叠成 TurnResponseItem 响应片段。
import type { TurnResponseItem } from "../../types/backend";
import {
  compareLifecycleEntities,
  isRecord,
  stringValue,
} from "./state";
import type {
  MessageStreamBlock,
  MessageStreamState,
  MessageStreamToolExecution,
} from "./types";

// reducer 和快照水合只替换参数对象；正文增量无需再次编码历史参数。
// 弱引用随原参数释放，不额外保留历史消息流。
const argumentJson = new WeakMap<object, string>();

function toolArgumentsText(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value) return "";
  if (typeof value !== "object") return JSON.stringify(value);
  const cached = argumentJson.get(value);
  if (cached !== undefined) return cached;
  const text = JSON.stringify(value);
  argumentJson.set(value, text);
  return text;
}

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
 * 展示层的工具状态，语义与 state 层 `toolExecutionStatusValue` 有意不同：
 * state 层把 outcome_unknown/provider_error 收敛为 `completed`，以便与后端
 * tool.completed 的归一结果保持一致（那是权威状态事实，不得改）；而展示层
 * 要告诉用户"结果未知/提供方出错，不能当作成功"，故降为 `failed`。
 * 二者不是同一原语，禁止合并趋同。
 */
function toolExecutionDisplayStatus(
  execution: MessageStreamToolExecution,
): "running" | "completed" | "failed" | "cancelled" {
  if (execution.outcome === "cancelled") return "cancelled";
  if (execution.status === "failed") return "failed";
  if (execution.outcome === "outcome_unknown" || execution.outcome === "provider_error") {
    return "failed";
  }
  return execution.status === "completed" ? "completed" : "running";
}

/** 沿原流身份投影已知执行证据，不从完成镜像时间补造真实停止边界。 */
function toolExecutionSummary(state: MessageStreamState, execution: MessageStreamToolExecution): TurnResponseItem["tool_execution"] {
  if (!execution.tool_invocation_id || !execution.tool_attempt_id
    || execution.started_seq === undefined) return undefined;
  return {
    session_id: state.sessionId, thread_id: state.threadId, turn_stream_id: state.turnStreamId,
    tool_invocation_id: execution.tool_invocation_id, tool_attempt_id: execution.tool_attempt_id,
    tool_execution_id: execution.tool_execution_id, status: execution.status, outcome: execution.outcome,
    started_seq: execution.started_seq, started_at: execution.started_at,
    execution_completed_at: execution.execution_completed_at,
  };
}

export function messageStreamToResponseItems(
  state: MessageStreamState,
): TurnResponseItem[] {
  const parts: TurnResponseItem[] = [];
  const executionCallIds = new Set(
    state.toolExecutions.map((execution) => execution.tool_call_id),
  );
  if (executionCallIds.size !== state.toolExecutions.length) throw new Error("同一工具调用存在多个attempt");
  const entities: MessageStreamResponseEntity[] = [
    ...state.blocks.map((block) => ({
      kind: "block" as const,
      value: block,
      fallback: block.block_index,
      id: block.block_id,
    })),
    ...state.toolExecutions.map((execution) => ({
      kind: "tool_execution" as const,
      value: execution,
      fallback: 0,
      id: execution.tool_execution_id,
    })),
    ...Object.entries(state.toolCalls)
      .filter(([toolCallId]) => !executionCallIds.has(toolCallId))
      .map(([id, value]) => ({
        kind: "tool_call" as const,
        value,
        fallback: 0,
        id,
      })),
  ];
  entities.sort((left, right) => compareLifecycleEntities(
    { ...left, value: left.kind === "tool_execution"
      ? { ...left.value, ...state.toolCalls[left.value.tool_call_id] } : left.value },
    { ...right, value: right.kind === "tool_execution"
      ? { ...right.value, ...state.toolCalls[right.value.tool_call_id] } : right.value },
  ));

  // 工具数组与正文 block 的 index 属于不同坐标域。先沿生命周期定位槽位，
  // 再仅替换同一模型调用的工具槽位，避免混合比较器产生非传递排序。
  const callGroups = new Map<string, { positions: number[]; calls: MessageStreamResponseEntity[] }>();
  for (const [position, entity] of entities.entries()) {
    if (entity.kind === "block") continue;
    const call = entity.kind === "tool_execution" ? state.toolCalls[entity.value.tool_call_id] : entity.value;
    if (typeof call?.model_call_id !== "string") continue;
    const group = callGroups.get(call.model_call_id) ?? { positions: [], calls: [] };
    group.positions.push(position);
    group.calls.push(entity);
    callGroups.set(call.model_call_id, group);
  }
  for (const { positions, calls } of callGroups.values()) {
    const indexOf = (entity: MessageStreamResponseEntity): number | undefined => {
      if (entity.kind === "block") return undefined;
      const call = entity.kind === "tool_execution" ? state.toolCalls[entity.value.tool_call_id] : entity.value;
      return typeof call?.provider_call_index === "number" ? call.provider_call_index : undefined;
    };
    // 没有来源顺序时保留已知顺序，不把到达顺序补成 Provider 顺序。
    if (calls.some(call => indexOf(call) === undefined)) continue;
    calls.sort((left, right) => indexOf(left)! - indexOf(right)!);
    positions.forEach((position, index) => { entities[position] = calls[index]; });
  }

  for (const entity of entities) {
    if (entity.kind === "block") {
      const block = entity.value;
      if (block.projection === "intermediate" || block.projection === "superseded") continue;
      const kind = block.redacted || block.carrier_type === "redacted_thinking"
        ? "reasoning_encrypted"
        : ["text", "output_text", "refusal"].includes(block.carrier_type)
          ? "text"
          : block.carrier_type === "reasoning_items"
            ? "reasoning_summary"
            : ["reasoning", "reasoning_content", "thinking"].includes(block.carrier_type)
              ? "reasoning" : "content";
      const structuredText = block.items
        .map((item) => stringValue(item.text) ?? stringValue(item.content) ?? stringValue(item.summary) ?? "")
        .filter(Boolean)
        .join("\n");
      const text = block.redacted ? "" : block.text || structuredText;
      if (text || block.redacted || block.items.length > 0) {
        parts.push({
          id: block.block_id,
          kind,
          projection: "streaming",
          status: block.status === "completed" || block.status === "running"
            ? block.status
            : "failed",
          source: {
            message_sequence: 0,
            content_block_index: block.block_index,
            model_call_id: block.model_call_id ?? undefined,
          },
          text,
          content: kind === "content" ? block.items[0] : undefined,
          carrier_type: block.carrier_type,
          completion_reason: block.completion_reason,
          partial: block.partial ?? false,
          // block完成仅表示该块封存，最终回答由权威Turn输出身份确认。
          final: false,
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
      const tool_execution = toolExecutionSummary(state, execution);
      parts.push({
        id: execution.tool_call_id,
        kind: "tool_call",
        projection: "streaming",
        status,
        source: { message_sequence: 0, model_call_id: stringValue(toolCall?.model_call_id) ?? undefined },
        text: "",
        tool_call_id: execution.tool_call_id,
        tool_name: execution.tool_name,
        tool_execution,
        arguments: toolArgumentsText(argumentsValue),
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
          id: `${execution.tool_call_id}:result`,
          kind: "tool_result",
          projection: "streaming",
          status,
          source: { message_sequence: 0, model_call_id: stringValue(toolCall?.model_call_id) ?? undefined },
          text: execution.result ?? execution.error ?? "",
          result: execution.result ?? execution.error ?? "",
          tool_call_id: execution.tool_call_id,
          tool_name: execution.tool_name,
          tool_execution,
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
      id: toolCallId,
      kind: "tool_call",
      projection: "streaming",
      status: callStatus === "cancelled" ? "cancelled"
        : callStatus === "incomplete" ? "failed"
          : toolCall.arguments_complete === true ? "pending" : "running",
      source: { message_sequence: 0, model_call_id: stringValue(toolCall.model_call_id) ?? undefined },
      text: "",
      tool_call_id: toolCallId,
      tool_name: stringValue(toolCall.tool_name) ?? "tool",
      arguments: typeof argumentsValue === "string" || isRecord(argumentsValue)
        ? toolArgumentsText(argumentsValue)
        : "",
      outcome_unknown: false,
      final: callStatus === "cancelled" || callStatus === "incomplete",
    });
  }
  return parts;
}
