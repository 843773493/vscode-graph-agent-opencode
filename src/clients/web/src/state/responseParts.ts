import type { TurnResponsePart } from "../types/backend";
import type { TimelineItem } from "./timelineTypes";

/** live 事件尚未落盘时没有 JSONL message sequence，只携带稳定的流式 part id。 */
export interface LiveResponsePart extends Omit<TurnResponsePart, "source"> {
  source: {
    message_sequence: null;
    stream_part_id: string;
  };
  raw_start?: Record<string, unknown>;
  raw_end?: Record<string, unknown>;
}

type ResponsePartLike = TurnResponsePart | LiveResponsePart;

function toolPartKey(part: ResponsePartLike): string {
  const source = part.source;
  const assistantSequence = "assistant_message_sequence" in source
    && typeof source.assistant_message_sequence === "number"
    ? source.assistant_message_sequence
    : source.message_sequence;
  const streamPartId = "stream_part_id" in source ? source.stream_part_id : null;
  const owner = assistantSequence ?? streamPartId ?? part.part_id;
  const order = source.call_index ?? part.tool_call_id ?? part.part_id;
  return `${owner}:${order}`;
}

function liveSource(partId: string): LiveResponsePart["source"] {
  return { message_sequence: null, stream_part_id: partId };
}

function liveToolStatus(item: Extract<TimelineItem, { kind: "aggregated_tool" }>) {
  if (item.active) return "running" as const;
  if (item.failed) return "failed" as const;
  return "completed" as const;
}

function liveToolCallId(
  item: Extract<TimelineItem, { kind: "aggregated_tool" }>,
): string | null {
  const value = item.rawEnd.tool_call_id ?? item.rawStart.tool_call_id;
  return typeof value === "string" && value.trim() ? value : null;
}

/**
 * 将 live 聚合结果适配为历史也使用的 response part。
 * trace 控制事件不在这里伪造成消息部件，由调用方原样保留给错误/中断渲染。
 */
export function liveTimelineItemsToResponseParts(
  items: readonly TimelineItem[],
): LiveResponsePart[] {
  const parts: LiveResponsePart[] = [];
  for (const item of items) {
    if (item.kind === "aggregated_text") {
      parts.push({
        part_id: item.id,
        kind: item.partKind === "reasoning" ? "reasoning" : "text",
        projection: "streaming",
        status: item.active ? "running" : "completed",
        source: liveSource(item.id),
        text: item.text,
        final: false,
      });
      continue;
    }
    if (item.kind !== "aggregated_tool") continue;
    const status = liveToolStatus(item);
    const toolCallId = liveToolCallId(item);
    parts.push({
      part_id: item.id,
      kind: "tool_call",
      projection: "streaming",
      status,
      source: liveSource(item.id),
      text: "",
      tool_call_id: toolCallId,
      tool_name: item.toolName,
      arguments: item.inputText,
      raw_start: item.rawStart,
      raw_end: item.rawEnd,
    });
    if (item.resultText) {
      parts.push({
        part_id: item.id,
        kind: "tool_result",
        projection: "streaming",
        status,
        source: liveSource(item.id),
        text: item.resultText,
        result: item.resultText,
        tool_call_id: toolCallId,
        tool_name: item.toolName,
        raw_start: item.rawStart,
        raw_end: item.rawEnd,
      });
    }
  }
  return parts;
}

/** 将历史 response parts 转为 live 也使用的时间线部件。 */
export function responsePartsToTimelineItems(
  parts: readonly ResponsePartLike[],
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
        const assistantSequence = part.source.assistant_message_sequence;
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
      const rawStart = "raw_start" in part && part.raw_start ? part.raw_start : {};
      const rawEnd = "raw_end" in part && part.raw_end ? part.raw_end : {};
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
        active: (
          part.status === "pending" || part.status === "running"
        ) && !resultKeys.has(toolPartKey(part))
          && !resultCallIds.has(part.tool_call_id ?? ""),
        failed: part.status === "failed",
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
            resultText: part.result ?? part.text,
            active: false,
            failed: part.status === "failed",
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
          rawStart: "raw_start" in part && part.raw_start ? part.raw_start : {},
          rawEnd: "raw_end" in part && part.raw_end ? part.raw_end : {},
          active: false,
          failed: part.status === "failed",
        });
      }
    }
  }
  return items;
}

/** 保留 live trace 的原始位置，同时让文本和工具经过统一 response-part 渲染路径。 */
export function liveTimelineItemsToRenderItems(
  items: readonly TimelineItem[],
): TimelineItem[] {
  const responseItems = responsePartsToTimelineItems(
    liveTimelineItemsToResponseParts(items),
  );
  const responseById = new Map(responseItems.map((item) => [item.id, item]));
  const rendered: TimelineItem[] = [];
  const emitted = new Set<string>();
  for (const item of items) {
    if (item.kind !== "aggregated_text" && item.kind !== "aggregated_tool") {
      rendered.push(item);
      continue;
    }
    const renderedItem = responseById.get(item.id);
    if (renderedItem && !emitted.has(renderedItem.id)) {
      rendered.push(renderedItem);
      emitted.add(renderedItem.id);
    }
  }
  return rendered;
}
