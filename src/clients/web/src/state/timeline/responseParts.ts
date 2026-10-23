import type { InternalExecutionHistory, TurnResponsePart } from "../../types/backend";
import type { TimelineItem } from "./timelineTypes";
import { isRecord } from "../../utils/jsonDisplay";

export interface ResponsePartsProjectionOptions {
  terminalFailure?: boolean;
  terminalCancellation?: boolean;
}

function internalResponsePartOrder(
  part: InternalExecutionHistory["response_parts"][number],
): [number, number] {
  const logicalItemOrdinal = part.source.logical_item_ordinal;
  const partOrdinal = part.source.part_ordinal;
  if (
    typeof logicalItemOrdinal !== "number"
    || !Number.isSafeInteger(logicalItemOrdinal)
    || logicalItemOrdinal < 0
    || typeof partOrdinal !== "number"
    || !Number.isSafeInteger(partOrdinal)
    || partOrdinal < 0
  ) {
    throw new Error("internal execution response part 缺少 canonical order");
  }
  return [logicalItemOrdinal, partOrdinal];
}

/** 按 execution response part 的成员坐标排序，缺少权威坐标时直接报错。 */
export function compareInternalResponseParts(
  left: InternalExecutionHistory["response_parts"][number],
  right: InternalExecutionHistory["response_parts"][number],
): number {
  const [leftLogicalOrdinal, leftPartOrdinal] = internalResponsePartOrder(left);
  const [rightLogicalOrdinal, rightPartOrdinal] = internalResponsePartOrder(right);
  return leftLogicalOrdinal - rightLogicalOrdinal
    || leftPartOrdinal - rightPartOrdinal
    || left.part_id.localeCompare(right.part_id);
}

function toolPartKey(part: TurnResponsePart): string {
  const source = part.source;
  const assistantSequence = "assistant_message_sequence" in source
    && typeof source.assistant_message_sequence === "number"
    ? source.assistant_message_sequence
    : source.message_sequence;
  const streamPartId = "stream_part_id" in source ? source.stream_part_id : null;
  const owner = assistantSequence ?? streamPartId ?? part.part_id;
  const order = ("call_index" in source ? source.call_index : undefined)
    ?? part.tool_call_id
    ?? part.part_id;
  return `${owner}:${order}`;
}

function recordField(
  part: TurnResponsePart,
  field: "raw_start" | "raw_end",
): Record<string, unknown> {
  const value = (part as unknown as Record<string, unknown>)[field];
  return isRecord(value) ? value : {};
}

/** 将权威 Turn response parts 转为聊天时间线部件。 */
export function responsePartsToTimelineItems(
  parts: readonly TurnResponsePart[],
  options: ResponsePartsProjectionOptions = {},
): TimelineItem[] {
  const items: TimelineItem[] = [];
  const toolIndexes = new Map<string, number>();
  const toolIndexesByCallId = new Map<string, number>();
  const toolResultKeys = new Set<string>();
  const toolResultCallIdsWithoutAssistantSequence = new Set<string>();
  for (const part of parts) {
    if (part.kind !== "tool_result") continue;
    toolResultKeys.add(toolPartKey(part));
    const assistantSequence = "assistant_message_sequence" in part.source
      ? part.source.assistant_message_sequence
      : undefined;
    if ((assistantSequence === undefined || assistantSequence === null) && part.tool_call_id) {
      toolResultCallIdsWithoutAssistantSequence.add(part.tool_call_id);
    }
  }

  for (const [partIndex, part] of parts.entries()) {
    const text = part.text ?? "";
    if (part.kind === "text" || part.kind === "final_text") {
      if (!text) continue;
      items.push({
        kind: "aggregated_text",
        id: part.part_id,
        text,
        partKind: "markdown",
        active: part.projection === "streaming" && part.status !== "completed",
        timestamp: null,
        eventCount: 1,
        rawEvents: [],
        responseSources: [{ part_id: part.part_id, kind: part.kind, source: part.source }],
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
      if (!text && part.kind !== "reasoning_encrypted") continue;
      const encrypted = part.kind === "reasoning_encrypted";
      items.push({
        kind: "aggregated_text",
        id: part.part_id,
        text,
        partKind: part.kind === "compaction_summary" ? "compaction" : "reasoning",
        ...(part.kind !== "compaction_summary" ? { reasoningKind: part.kind } : {}),
        redacted: encrypted || part.carrier_type?.includes("redacted_thinking") === true,
        active: part.projection === "streaming" && part.status !== "completed",
        timestamp: null,
        eventCount: 1,
        rawEvents: [],
        responseSources: [{ part_id: part.part_id, kind: part.kind, source: part.source }],
      });
      continue;
    }
    if (part.kind === "tool_call") {
      const rawStart = recordField(part, "raw_start");
      const rawEnd = recordField(part, "raw_end");
      const unresolvedTerminalTool = options.terminalFailure === true
        && part.status !== "failed"
        && !toolResultKeys.has(toolPartKey(part))
        && !toolResultCallIdsWithoutAssistantSequence.has(part.tool_call_id ?? "");
      const incomplete = part.partial === true
        || part.status === "cancelled"
        || (options.terminalCancellation === true
          && part.outcome_unknown === true
          && !toolResultKeys.has(toolPartKey(part))
          && !toolResultCallIdsWithoutAssistantSequence.has(part.tool_call_id ?? ""));
      const item: TimelineItem = {
        kind: "aggregated_tool",
        id: part.part_id,
        toolName: part.tool_name ?? "tool",
        toolCallId: part.tool_call_id ?? undefined,
        inputText: part.arguments ?? "",
        resultText: text,
        timestamp: null,
        rawStart,
        rawEnd,
        // 历史投影中 tool_call 可能保留 running 状态，但只要同一批
        // response parts 已有 tool_result，就必须按已完成工具展示。
        active: !unresolvedTerminalTool && (
          part.status === "pending" || part.status === "running"
        ) && !toolResultKeys.has(toolPartKey(part))
          && !toolResultCallIdsWithoutAssistantSequence.has(part.tool_call_id ?? ""),
        detailsLoaded: part.projection === "detail",
        failed: !incomplete && (part.status === "failed" || unresolvedTerminalTool),
        incomplete,
        outcomeUnknown: !incomplete && (part.outcome_unknown === true || unresolvedTerminalTool),
        responseSources: [{ part_id: part.part_id, kind: part.kind, source: part.source }],
      };
      const index = items.length;
      toolIndexes.set(toolPartKey(part), index);
      if (part.tool_call_id) {
        toolIndexesByCallId.set(part.tool_call_id, index);
      }
      items.push(item);
      continue;
    }
    if (part.kind === "tool_result") {
      const key = toolPartKey(part);
      const assistantSequence = "assistant_message_sequence" in part.source
        ? part.source.assistant_message_sequence
        : undefined;
      const existingIndex = toolIndexes.get(key)
        ?? (assistantSequence == null && part.tool_call_id
          ? toolIndexesByCallId.get(part.tool_call_id)
          : undefined);
      const existing = existingIndex === undefined ? undefined : items[existingIndex];
      const status = {
        active: false,
        failed: (existing?.kind === "aggregated_tool" && existing.failed === true)
          || part.status === "failed",
        incomplete: part.status === "cancelled",
        outcomeUnknown: part.outcome_unknown === true,
      };
      if (existingIndex !== undefined && existing?.kind === "aggregated_tool") {
        const completed = { ...existing, ...status };
        // 只有相邻的调用与结果合并同卡；跨部件的结果保留权威顺序。
        if (parts[partIndex - 1]?.part_id === existing.id) {
          items[existingIndex] = {
            ...completed,
            resultText: part.result ?? part.text ?? "",
            detailsLoaded: existing.detailsLoaded || part.projection === "detail",
            responseSources: [...(existing.responseSources ?? []), { part_id: part.part_id, kind: part.kind, source: part.source }],
          };
          continue;
        }
        items[existingIndex] = completed;
      }
      items.push({
        kind: "aggregated_tool",
        id: part.part_id,
        toolName: part.tool_name
          ?? (existing?.kind === "aggregated_tool" ? existing.toolName : "tool"),
        toolCallId: part.tool_call_id ?? undefined,
        inputText: "",
        resultText: part.result ?? text,
        timestamp: null,
        rawStart: recordField(part, "raw_start"),
        rawEnd: recordField(part, "raw_end"),
        detailsLoaded: part.projection === "detail",
        ...status,
        responseSources: [{ part_id: part.part_id, kind: part.kind, source: part.source }],
      });
    }
  }
  return items;
}
