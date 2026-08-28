import type { TurnResponsePart } from "../types/backend";
import type { TimelineItem } from "./timelineTypes";

export interface ResponsePartsProjectionOptions {
  terminalFailure?: boolean;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 将权威 Turn response parts 转为聊天时间线部件。 */
export function responsePartsToTimelineItems(
  parts: readonly TurnResponsePart[],
  options: ResponsePartsProjectionOptions = {},
): TimelineItem[] {
  const items: TimelineItem[] = [];
  const toolIndexes = new Map<string, number>();
  const toolIndexesByCallId = new Map<string, number>();
  const resultKeys = new Set(
    parts
      .filter((part) => part.kind === "tool_result")
      .map((part) => toolPartKey(part)),
  );
  const resultCallIds = new Set(
    parts
      .filter((part) => part.kind === "tool_result")
      .filter((part) => {
        const assistantSequence = "assistant_message_sequence" in part.source
          ? part.source.assistant_message_sequence
          : undefined;
        return assistantSequence === undefined || assistantSequence === null;
      })
      .map((part) => part.tool_call_id)
      .filter((value): value is string => Boolean(value)),
  );

  for (const part of parts) {
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
      });
      continue;
    }
    if (
      part.kind === "reasoning"
      || part.kind === "reasoning_summary"
      || part.kind === "reasoning_encrypted"
    ) {
      if (!text) continue;
      items.push({
        kind: "aggregated_text",
        id: part.part_id,
        text,
        partKind: "reasoning",
        active: part.projection === "streaming" && part.status !== "completed",
        timestamp: null,
        eventCount: 1,
        rawEvents: [],
      });
      continue;
    }
    if (part.kind === "tool_call") {
      const rawStart = recordField(part, "raw_start");
      const rawEnd = recordField(part, "raw_end");
      const unresolvedTerminalTool = options.terminalFailure === true
        && !resultKeys.has(toolPartKey(part))
        && !resultCallIds.has(part.tool_call_id ?? "");
      const item: TimelineItem = {
        kind: "aggregated_tool",
        id: part.part_id,
        toolName: part.tool_name ?? "tool",
        inputText: part.arguments ?? "",
        resultText: text,
        timestamp: null,
        rawStart,
        rawEnd,
        // 历史投影中 tool_call 可能保留 running 状态，但只要同一批
        // response parts 已有 tool_result，就必须按已完成工具展示。
        active: !unresolvedTerminalTool && (
          part.status === "pending" || part.status === "running"
        ) && !resultKeys.has(toolPartKey(part))
          && !resultCallIds.has(part.tool_call_id ?? ""),
        failed: part.status === "failed" || unresolvedTerminalTool,
        outcomeUnknown: part.outcome_unknown === true || unresolvedTerminalTool,
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
      const existingIndex = part.tool_call_id
        ? toolIndexesByCallId.get(part.tool_call_id) ?? toolIndexes.get(key)
        : toolIndexes.get(key);
      if (existingIndex !== undefined) {
        const existing = items[existingIndex];
        if (existing.kind === "aggregated_tool") {
          items[existingIndex] = {
            ...existing,
            resultText: part.result ?? part.text ?? "",
            active: false,
            failed: part.status === "failed",
            outcomeUnknown: part.outcome_unknown === true,
          };
        }
      } else {
        items.push({
          kind: "aggregated_tool",
          id: part.part_id,
          toolName: part.tool_name ?? "tool",
          inputText: "",
          resultText: part.result ?? text,
          timestamp: null,
          rawStart: recordField(part, "raw_start"),
          rawEnd: recordField(part, "raw_end"),
          active: false,
          failed: part.status === "failed",
          outcomeUnknown: part.outcome_unknown === true,
        });
      }
    }
  }
  return items;
}
