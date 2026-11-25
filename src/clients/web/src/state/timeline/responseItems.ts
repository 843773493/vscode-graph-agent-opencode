import type { InternalExecutionHistory, TurnResponseItem } from "../../types/backend";
import type { TimelineItem } from "./timelineTypes";
import { isRecord } from "../../utils/jsonDisplay";
import { contextNode, encodeNode } from "./transcriptNodes";

export interface ResponseItemsProjectionOptions {
  terminalFailure?: boolean;
  terminalCancellation?: boolean;
}

function internalResponseItemOrder(
  part: InternalExecutionHistory["response_items"][number],
): [number, number] {
  const logicalItemOrdinal = part.kind === "context_notice" ? part.context_anchor_ordinal : part.source.logical_item_ordinal;
  const sourceOrdinal = part.kind === "context_notice" ? part.context_plan_ordinal : 0;
  if (
    typeof logicalItemOrdinal !== "number"
    || !Number.isSafeInteger(logicalItemOrdinal)
    || logicalItemOrdinal < 0
    || typeof sourceOrdinal !== "number"
    || !Number.isSafeInteger(sourceOrdinal)
    || sourceOrdinal < 0
  ) {
    throw new Error("internal execution response Item 缺少来源顺序");
  }
  return [logicalItemOrdinal, sourceOrdinal];
}

/** 按 execution response part 的成员坐标排序，缺少权威坐标时直接报错。 */
export function compareInternalResponseItems(
  left: InternalExecutionHistory["response_items"][number],
  right: InternalExecutionHistory["response_items"][number],
): number {
  const [leftLogicalOrdinal, leftPartOrdinal] = internalResponseItemOrder(left);
  const [rightLogicalOrdinal, rightPartOrdinal] = internalResponseItemOrder(right);
  return leftLogicalOrdinal - rightLogicalOrdinal
    || leftPartOrdinal - rightPartOrdinal
    || left.id.localeCompare(right.id);
}

function toolAssociationKeys(part: TurnResponseItem): string[] {
  const keys: string[] = [];
  const modelCall = part.source.model_call_id;
  if (modelCall && part.tool_call_id) {
    const prefix = `${modelCall}:tool-call:`;
    const callId = part.tool_call_id.startsWith(prefix) ? part.tool_call_id.slice(prefix.length) : part.tool_call_id;
    keys.push(JSON.stringify(["model", modelCall, callId]));
  } else if (part.source.assistant_message_sequence != null && part.tool_call_id) {
    // 只接受明确的原 assistant 归属，普通 message_sequence 不是调用身份。
    keys.push(JSON.stringify(["assistant", part.source.assistant_message_sequence, part.tool_call_id]));
  }
  const execution = part.tool_execution;
  if (execution) keys.push(JSON.stringify(["attempt", execution.session_id, execution.thread_id,
    execution.turn_stream_id, execution.tool_invocation_id, execution.tool_attempt_id]));
  return keys;
}

type ToolItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;

/** 只派生浏览器可直接展示的媒体来源；未知结构仍保留原单块内容供用户查看。 */
function outputMedia(content: TurnResponseItem["content"]): Extract<TimelineItem, {kind: "aggregated_text"}>["media"] {
  if (!content) return undefined;
  const type = typeof content.type === "string" ? content.type : "";
  const kind = type.startsWith("image") ? "image" : type.startsWith("audio") ? "audio" : type.startsWith("video") ? "video" : undefined;
  if (!kind) return undefined;
  const reference: unknown = content[`${kind}_url`] ?? content.source;
  const block = isRecord(reference) ? reference : content;
  const url: unknown = typeof reference === "string" ? reference : block.url;
  if (typeof url === "string" && (/^https?:\/\//i.test(url) || url.startsWith(`data:${kind}/`))) {
    return {kind, source: url};
  }
  const mime: unknown = block.media_type ?? block.mime_type;
  if (typeof block.data === "string" && typeof mime === "string" && mime.startsWith(`${kind}/`)
    && /^[\w.+-]+\/[\w.+-]+$/.test(mime)) return {kind, source: `data:${mime};base64,${block.data}`};
  return undefined;
}

function toolState(part: TurnResponseItem, result: TurnResponseItem | undefined, options: ResponseItemsProjectionOptions):
  Pick<ToolItem, "active" | "declared" | "failed" | "incomplete" | "outcomeUnknown"> {
  const execution = result?.tool_execution ?? part.tool_execution;
  const outcome = result?.tool_outcome ?? part.tool_outcome;
  if (outcome != null) {
    return { active: false, declared: false, failed: outcome === "failure",
      incomplete: outcome === "cancelled", outcomeUnknown: outcome === "unknown" };
  }
  const hasResult = result !== undefined || part.kind === "tool_result";
  const terminalExecution = execution != null && execution.status !== "running";
  const unresolvedTerminal = options.terminalFailure === true && !hasResult && !terminalExecution;
  const incomplete = execution?.outcome === "cancelled" || (result ?? part).status === "cancelled"
    || (!hasResult && !terminalExecution && (part.partial === true
      || (options.terminalCancellation === true && part.outcome_unknown === true)));
  const outcomeUnknown = (hasResult && (result ?? part).outcome_unknown === true) || !incomplete && ((!hasResult && !terminalExecution && part.outcome_unknown === true)
    || execution?.outcome === "outcome_unknown" || execution?.outcome === "execution_lost"
    || (terminalExecution && execution.outcome == null && !hasResult) || unresolvedTerminal);
  const failed = !incomplete && ((!execution && part.status === "failed") || result?.status === "failed"
    || execution?.status === "failed" || execution?.outcome === "provider_error" || unresolvedTerminal);
  const active = execution?.status === "running" && !hasResult && !incomplete && !unresolvedTerminal;
  return { active, failed, incomplete, outcomeUnknown,
    declared: !active && !failed && !incomplete && !outcomeUnknown && !hasResult && !terminalExecution };
}

function recordField(
  part: TurnResponseItem,
  field: "raw_start" | "raw_end",
): Record<string, unknown> {
  const value = (part as unknown as Record<string, unknown>)[field];
  return isRecord(value) ? value : {};
}

/** 将权威 Turn response parts 转为聊天时间线部件。 */
export function responseItemsToTimelineItems(
  parts: readonly TurnResponseItem[],
  options: ResponseItemsProjectionOptions = {},
): TimelineItem[] {
  const items: TimelineItem[] = [];
  const callsByIdentity = new Map<string, TurnResponseItem>();
  const callsByResult = new Map<string, TurnResponseItem>();
  const resultsByCall = new Map<string, TurnResponseItem>();
  for (const part of parts) {
    if (part.kind !== "tool_call") continue;
    for (const key of toolAssociationKeys(part)) {
      const previous = callsByIdentity.get(key);
      if (previous && previous.id !== part.id) throw new Error("工具调用身份对应多个声明");
      callsByIdentity.set(key, part);
    }
  }
  for (const part of parts) {
    if (part.kind !== "tool_result") continue;
    for (const key of toolAssociationKeys(part)) {
      const call = callsByIdentity.get(key);
      if (!call) continue;
      if (call.source.model_call_id && part.source.model_call_id
        && call.source.model_call_id !== part.source.model_call_id) throw new Error("工具结果与声明的ModelCall不一致");
      if (call.tool_execution && part.tool_execution
        && (call.tool_execution.tool_invocation_id !== part.tool_execution.tool_invocation_id
          || call.tool_execution.tool_attempt_id !== part.tool_execution.tool_attempt_id)) throw new Error("工具结果与声明的attempt不一致");
      const previous = callsByResult.get(part.id);
      if (previous && previous.id !== call.id) throw new Error("工具结果来源关联冲突");
      const previousResult = resultsByCall.get(call.id);
      if (previousResult && previousResult.id !== part.id) throw new Error("同一调用存在多个结果");
      callsByResult.set(part.id, call);
      resultsByCall.set(call.id, part);
    }
  }

  for (const part of parts) {
    const text = part.text ?? "";
    if (part.kind === "text" || part.kind === "final_text" || part.kind === "content") {
      if (!text && part.kind !== "content") continue;
      items.push({
        kind: "aggregated_text",
        id: part.id,
        text,
        partKind: part.kind === "content" ? "content" : "markdown",
        content: part.content, carrierType: part.carrier_type, truncated: part.truncated,
        media: part.kind === "content" ? outputMedia(part.content) : undefined,
        active: part.projection === "streaming" && part.status !== "completed",
        timestamp: null,
        eventCount: 1,
        rawEvents: [],
        responseSources: [{ id: part.id, kind: part.kind, source: part.source, projection: part.projection, final: part.final, tool_call_id: part.tool_call_id }],
      });
      continue;
    }
    if (
      part.kind === "reasoning"
      || part.kind === "reasoning_summary"
      || part.kind === "reasoning_encrypted"
      || part.kind === "compaction_summary"
    ) {
      // redacted/encrypted reasoning 可能只有存在性和元数据，没有可读正文；
      // 不能因 text 为空丢掉该时间线项，否则前端无法显示隐藏提示。
      if (!text && part.projection !== "summary" && part.kind !== "reasoning_encrypted") continue;
      const encrypted = part.kind === "reasoning_encrypted";
      items.push({
        kind: "aggregated_text",
        id: part.id,
        text,
        partKind: part.kind === "compaction_summary" ? "compaction" : "reasoning",
        ...(part.kind !== "compaction_summary" ? { reasoningKind: part.kind } : {}),
        redacted: encrypted || part.carrier_type?.includes("redacted_thinking") === true,
        active: part.projection === "streaming" && part.status !== "completed",
        timestamp: null,
        eventCount: 1,
        rawEvents: [],
        responseSources: [{ id: part.id, kind: part.kind, source: part.source, projection: part.projection, tool_call_id: part.tool_call_id }],
      });
      continue;
    }
    if (part.kind === "tool_call") {
      const result = resultsByCall.get(part.id);
      const rawStart = recordField(part, "raw_start");
      const rawEnd = recordField(result ?? part, "raw_end");
      const item: TimelineItem = {
        kind: "aggregated_tool",
        id: part.id,
        toolName: part.tool_name ?? "tool",
        toolCallId: part.tool_call_id ?? undefined,
        inputText: part.arguments ?? "",
        resultText: result?.result ?? result?.text ?? text,
        timestamp: null,
        rawStart,
        rawEnd,
        ...toolState(part, result, options),
        detailsLoaded: part.projection === "detail" && (result == null || result.projection === "detail"),
        responseSources: (result ? [part, result] : [part]).map(source => ({
          id: source.id, kind: source.kind, source: source.source,
          projection: source.projection, tool_call_id: source.tool_call_id,
        })),
      };
      items.push(item);
      continue;
    }
    if (part.kind === "tool_result") {
      // 精确关联的回执已在原调用位置展示；独立Item来源仍由工具输出节点保留。
      // 未加载声明或无法关联的结果继续在自身权威位置独立显示。
      if (callsByResult.has(part.id)) continue;
      items.push({
        kind: "aggregated_tool",
        id: part.id,
        toolName: part.tool_name ?? "tool",
        toolCallId: part.tool_call_id ?? undefined,
        inputText: "",
        resultText: part.result ?? text,
        timestamp: null,
        rawStart: recordField(part, "raw_start"),
        rawEnd: recordField(part, "raw_end"),
        detailsLoaded: part.projection === "detail",
        ...toolState(part, part, options),
        responseSources: [{ id: part.id, kind: part.kind, source: part.source, projection: part.projection, tool_call_id: part.tool_call_id }],
      });
    }
  }
  for (const { id, notices } of contextNoticeGroups(parts, "before_model_call")) {
    // 补页可能还携带输出产生前的坐标；已有真实输出的成员决定展示位置。
    const position = notices.find(notice => notice.context_before_item_id) ?? notices[0];
    const call = position.context_model_call_id;
    const beforeId = position.context_before_item_id;
    let index = beforeId ? items.findIndex(item => item.responseSources?.some(source => source.source.item_id === beforeId)) : -1;
    if (index < 0) index = items.findIndex(item => item.responseSources?.some(source => source.source.model_call_id === call));
    if (index < 0) {
      const ordinal = position.context_anchor_ordinal;
      index = items.findIndex(item => item.responseSources?.some(source => ordinal != null
        && source.source.logical_item_ordinal != null && source.source.logical_item_ordinal >= ordinal));
    }
    items.splice(index < 0 ? items.length : index, 0, { kind: "context_event", id, notices });
  }
  return items;
}

/** 分组与阅读锚点使用同一来源身份，插入位置变化不拆分标记。 */
export function contextNoticeGroups(parts: readonly TurnResponseItem[], anchor: "before_user_message" | "before_model_call") {
  const groups = new Map<string, TurnResponseItem[]>();
  for (const part of parts) {
    if (part.kind !== "context_notice" || part.context_anchor !== anchor) continue;
    const key = encodeNode(contextNode(part));
    const group = groups.get(key);
    if (group) group.push(part); else groups.set(key, [part]);
  }
  return [...groups].map(([key, notices]) => ({ id: `context:${key}`, notices }));
}

/** 调用方已核验同一view绑定；正文保留与执行元数据更新使用同一合并入口。 */
export function mergeCanonicalItem(previous: TurnResponseItem | undefined, incoming: TurnResponseItem): TurnResponseItem {
  if (!previous) return incoming;
  const before = previous.tool_execution;
  const after = incoming.tool_execution;
  if (before && after && (before.session_id !== after.session_id || before.thread_id !== after.thread_id
    || before.turn_stream_id !== after.turn_stream_id || before.tool_invocation_id !== after.tool_invocation_id
    || before.tool_attempt_id !== after.tool_attempt_id)) throw new Error("同一调用的执行摘要身份冲突");
  const knownOutcome = before?.outcome === "success" || before?.outcome === "provider_error" || before?.outcome === "cancelled";
  const lessInformation = after?.outcome == null || after.outcome === "outcome_unknown" || after.outcome === "execution_lost";
  const execution = before && (!after || (before.status !== "running" && (after.status === "running" || (knownOutcome && lessInformation))))
    ? before : after;
  const tool_execution = execution && { ...execution,
    started_at: execution.started_at ?? before?.started_at ?? after?.started_at,
    execution_completed_at: execution.execution_completed_at ?? before?.execution_completed_at ?? after?.execution_completed_at };
  const merged = previous.projection === "detail" && incoming.projection === "summary"
    ? { ...previous, ...incoming, projection: "detail" as const, text: previous.text, content: previous.content,
      arguments: previous.arguments, result: previous.result, truncated: previous.truncated }
    : incoming;
  const tool_outcome = previous.tool_outcome != null && (incoming.tool_outcome == null
    || previous.tool_outcome !== "unknown" && incoming.tool_outcome === "unknown")
    ? previous.tool_outcome : incoming.tool_outcome;
  return { ...merged, tool_execution, tool_outcome };
}

/** 片段只覆盖已声明的 canonical 内容，同绑定轻摘要不能降级已读正文。 */
export function mergeCanonicalItems(current: readonly TurnResponseItem[], incoming: readonly TurnResponseItem[]): TurnResponseItem[] {
  const parts = new Map(current.map(part => [part.id, part]));
  for (const part of incoming) {
    const previous = parts.get(part.id);
    parts.set(part.id, mergeCanonicalItem(previous, part));
  }
  return [...parts.values()].sort(compareInternalResponseItems);
}
