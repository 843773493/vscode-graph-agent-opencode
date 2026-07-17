import type { TraceEvent } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import {
  createSkillKeyFlowState,
  keyFlowSkillNames,
  recordFinalText,
  recordKeyFlowToolCall,
  recordKeyFlowToolResult,
  recordReadSkill,
  skillKeyFlowSnapshot,
} from "../skillKeyFlow";
import type { TimelineItem } from "../timelineTypes";
import { extractEventInfo, getOptionalString } from "../tracePayload";
import {
  buildActiveToolItem,
  buildCompletedToolItem,
  buildFailedToolItem,
  traceErrorSignature,
  traceToolArgs,
  type PendingToolCall,
} from "./toolAggregation";

type TextPart = Extract<TimelineItem, { kind: "aggregated_text" }>;

function requiredPartId(partId: string | null, eventType: string): string {
  if (!partId) {
    throw new Error(`事件 ${eventType} 缺少 part_id`);
  }
  return partId;
}

function requiredPartKind(
  payload: Record<string, unknown>,
  eventType: string,
): TextPart["partKind"] {
  const kind = getOptionalString(payload, "kind");
  if (kind !== "markdown" && kind !== "reasoning") {
    throw new Error(`事件 ${eventType} 的 kind 必须是 markdown 或 reasoning`);
  }
  return kind;
}

/** 按 part_id 原地合并增量，并保持各 part 第一次出现时的顺序。 */
export function aggregateConversationEvents(
  events: TraceEvent[],
  convId: string,
  isRunning: boolean,
): TimelineItem[] {
  const items: TimelineItem[] = [];
  const seenEventIds = new Set<string>();
  const textPartIndexes = new Map<string, number>();
  const textPartSegmentStarts = new Map<string, number>();
  const pendingToolCalls = new Map<string, PendingToolCall>();
  const skillFlow = createSkillKeyFlowState();
  let hasOutputContent = false;
  let llmModel = "";
  let agentEndTs: string | null = null;
  let sawSuccessfulTerminalEvent = false;
  let sawSessionInterrupted = false;
  let lastErrorSignature = "";

  function addTraceItem(
    eventType: string,
    payload: Record<string, unknown>,
    timestamp: string | null,
    eventId: string | null,
  ) {
    items.push({
      kind: "trace",
      id: eventId ? `trace-${eventId}` : `trace-${eventType}-${timestamp ?? items.length}`,
      eventType,
      payload,
      timestamp,
    });
  }

  function textPart(partId: string, eventType: string): TextPart {
    const index = textPartIndexes.get(partId);
    const item = index === undefined ? undefined : items[index];
    if (!item || item.kind !== "aggregated_text") {
      throw new Error(`事件 ${eventType} 引用了尚未开始的 part_id=${partId}`);
    }
    return item;
  }

  function replaceTextPart(partId: string, item: TextPart) {
    const index = textPartIndexes.get(partId);
    if (index === undefined) {
      throw new Error(`无法更新不存在的文本 part_id=${partId}`);
    }
    items[index] = item;
  }

  function failPendingTools(payload: Record<string, unknown>, eventType: string) {
    for (const pending of pendingToolCalls.values()) {
      items[pending.itemIndex] = buildFailedToolItem(pending, payload, eventType);
    }
    pendingToolCalls.clear();
  }

  for (const event of events) {
    const { eventType, payload, timestamp, eventId, partId } = extractEventInfo(event);
    if (eventId && seenEventIds.has(eventId)) {
      continue;
    }
    if (eventId) {
      seenEventIds.add(eventId);
    }
    const type = eventType.toLowerCase();

    if (type === "llm_request") {
      llmModel = getOptionalString(payload, "model") || llmModel;
    }
    if (type === "agent_end") {
      agentEndTs = timestamp;
      sawSuccessfulTerminalEvent = true;
    }
    if (type === "job_completed") {
      sawSuccessfulTerminalEvent = true;
    }

    if (type === "text_start") {
      const id = requiredPartId(partId, eventType);
      if (textPartIndexes.has(id)) {
        const current = textPart(id, eventType);
        const kind = requiredPartKind(payload, eventType);
        if (current.partKind !== kind) {
          throw new Error(`part_id=${id} 的 kind 从 ${current.partKind} 变成了 ${kind}`);
        }
        textPartSegmentStarts.set(id, current.text.length);
        replaceTextPart(id, {
          ...current,
          active: isRunning,
          eventCount: current.eventCount + 1,
          rawEvents: [...current.rawEvents, { type: eventType, payload }],
        });
        continue;
      }
      const item: TextPart = {
        kind: "aggregated_text",
        id,
        text: "",
        partKind: requiredPartKind(payload, eventType),
        active: isRunning,
        timestamp,
        eventCount: 1,
        rawEvents: [{ type: eventType, payload }],
      };
      textPartIndexes.set(id, items.length);
      textPartSegmentStarts.set(id, 0);
      items.push(item);
      continue;
    }

    if (type === "text_delta") {
      const id = requiredPartId(partId, eventType);
      const current = textPart(id, eventType);
      const kind = requiredPartKind(payload, eventType);
      if (current.partKind !== kind) {
        throw new Error(`part_id=${id} 的 kind 从 ${current.partKind} 变成了 ${kind}`);
      }
      const next = {
        ...current,
        text: current.text + getOptionalString(payload, "text"),
        active: isRunning,
        eventCount: current.eventCount + 1,
        rawEvents: [...current.rawEvents, { type: eventType, payload }],
      };
      replaceTextPart(id, next);
      if (next.text.trim()) {
        hasOutputContent = true;
      }
      continue;
    }

    if (type === "text_end") {
      const id = requiredPartId(partId, eventType);
      const current = textPart(id, eventType);
      const kind = requiredPartKind(payload, eventType);
      if (current.partKind !== kind) {
        throw new Error(`part_id=${id} 的结束 kind 与开始 kind 不一致`);
      }
      const finalText = getOptionalString(payload, "text");
      const segmentStart = textPartSegmentStarts.get(id) ?? 0;
      const streamedSegment = current.text.slice(segmentStart);
      const resolvedText = !finalText || finalText === streamedSegment
        ? current.text
        : segmentStart > 0
          ? current.text.slice(0, segmentStart) + finalText
          : finalText;
      const next = {
        ...current,
        text: resolvedText,
        active: false,
        eventCount: current.eventCount + 1,
        rawEvents: [...current.rawEvents, { type: eventType, payload }],
      };
      replaceTextPart(id, next);
      if (kind === "markdown") {
        recordFinalText(skillFlow, next.text);
      }
      if (next.text.trim()) {
        hasOutputContent = true;
      }
      continue;
    }

    if (type === "tool_call_start") {
      const id = requiredPartId(partId, eventType);
      if (pendingToolCalls.has(id)) {
        throw new Error(`事件 ${eventType} 重复开始 part_id=${id}`);
      }
      const toolName = getOptionalString(payload, "tool_name");
      const args = traceToolArgs(payload);
      recordReadSkill(skillFlow, { toolName, args });
      recordKeyFlowToolCall(skillFlow, {
        toolName,
        skillNames: keyFlowSkillNames(payload.skill_names),
        invocationToolName: getOptionalString(payload, "invocation_tool_name"),
      });
      const pending: PendingToolCall = {
        partId: id,
        itemIndex: items.length,
        payload,
        timestamp,
      };
      pendingToolCalls.set(id, pending);
      items.push(buildActiveToolItem(pending));
      hasOutputContent = true;
      continue;
    }

    if (type === "tool_call_end") {
      const id = requiredPartId(partId, eventType);
      const pending = pendingToolCalls.get(id);
      if (!pending) {
        throw new Error(`事件 ${eventType} 引用了尚未开始的 part_id=${id}`);
      }
      pendingToolCalls.delete(id);
      const completedToolItem = buildCompletedToolItem(pending, payload);
      items[pending.itemIndex] = completedToolItem;
      const startPayload = pending.payload;
      recordKeyFlowToolResult(skillFlow, {
        toolName: completedToolItem.toolName,
        skillNames: Array.from(new Set([
          ...keyFlowSkillNames(startPayload.skill_names),
          ...keyFlowSkillNames(payload.skill_names),
        ])),
        resultText: completedToolItem.resultText,
        invocationToolName:
          getOptionalString(startPayload, "invocation_tool_name") ||
          getOptionalString(payload, "invocation_tool_name"),
      });
      continue;
    }

    if (type === "session_interrupted") {
      hasOutputContent = true;
      sawSessionInterrupted = true;
      addTraceItem(eventType, payload, timestamp, eventId);
      continue;
    }

    if (
      sawSessionInterrupted &&
      ((type === "error" && getOptionalString(payload, "error") === "任务被取消") ||
        type === "job_cancelled")
    ) {
      continue;
    }

    if (type === "error" || type === "job_failed" || type === "job_cancelled") {
      if (pendingToolCalls.size > 0) {
        failPendingTools(payload, eventType);
      }
      hasOutputContent = true;
      const signature = traceErrorSignature(payload) || type;
      if (signature !== lastErrorSignature) {
        addTraceItem(eventType, payload, timestamp, eventId);
        lastErrorSignature = signature;
      }
      continue;
    }

    if (
      type === "job_completed" ||
      type === "status_change" ||
      type === "job_created" ||
      type === "job_started" ||
      type === "agent_start" ||
      type === "agent_step" ||
      type === "llm_request" ||
      type === "agent_end" ||
      type === "message_created"
    ) {
      continue;
    }

    hasOutputContent = true;
    addTraceItem(eventType, payload, timestamp, eventId);
  }

  if (!isRunning) {
    for (const [partId, index] of textPartIndexes) {
      const item = items[index];
      if (item.kind === "aggregated_text" && item.active) {
        items[index] = { ...item, active: false };
      }
      textPartIndexes.set(partId, index);
    }
    if (pendingToolCalls.size > 0) {
      failPendingTools(
        { error: "工具调用缺少完成事件，无法恢复最终状态。" },
        "history_incomplete",
      );
    }
  }

  if (
    sawSuccessfulTerminalEvent &&
    !isRunning &&
    !hasOutputContent &&
    items.length === 0
  ) {
    const modelInfo = llmModel ? `模型: ${llmModel}` : "";
    items.push({
      kind: "trace",
      id: `fallback-agent-end-${convId}-${agentEndTs || items.length}`,
      eventType: "llm_empty_response",
      payload: { message: modelInfo || "LLM 返回了空响应", model: llmModel },
      timestamp: agentEndTs,
    });
  }

  const skillSnapshot = skillKeyFlowSnapshot(skillFlow);
  if (skillSnapshot.keyFlowToolResults.length > 0 && skillSnapshot.finalText) {
    const summaryItem: TimelineItem = {
      kind: "skill_summary",
      id: `skill-summary-${convId}`,
      readSkills: skillSnapshot.readSkills,
      toolResults: skillSnapshot.keyFlowToolResults.map((item) => ({
        toolName: item.toolName,
        skillNames: item.skillNames,
        invocationToolName: item.invocationToolName,
        resultText: item.resultText,
      })),
      finalText: skillSnapshot.finalText,
      timestamp: agentEndTs,
    };
    const finalTextIndex = items.findIndex(
      (item) => item.kind === "aggregated_text" && item.partKind === "markdown" && !item.active,
    );
    if (finalTextIndex === -1) {
      items.push(summaryItem);
    } else {
      items.splice(finalTextIndex, 0, summaryItem);
    }
  }

  return items.filter(
    (item) => item.kind !== "aggregated_text" || Boolean(item.text.trim()),
  );
}

export function buildPendingStatusItem(
  conv: ConversationView,
): Extract<TimelineItem, { kind: "status" }> | null {
  if (conv.status !== "running" && conv.status !== "queued") {
    return null;
  }

  const events = conv.events;
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const lastPayload = lastEvent ? extractEventInfo(lastEvent).payload : {};
  const timestamp = lastEvent?.timestamp ?? conv.userMessage?.created_at ?? null;

  if (conv.status === "queued") {
    return {
      kind: "status",
      id: `${conv.conversationId}-queued`,
      title: "已排队",
      detail: "上一轮还在执行，这条消息会自动接着运行。",
      timestamp,
    };
  }

  if (lastEvent?.type === "llm_request") {
    const model = getOptionalString(lastPayload, "model");
    return {
      kind: "status",
      id: `${conv.conversationId}-llm-request`,
      title: "模型响应中",
      detail: model ? `正在请求模型：${model}` : "正在请求模型。",
      timestamp,
    };
  }

  if (lastEvent?.type === "tool_call_start") {
    const toolName = getOptionalString(lastPayload, "tool_name") || "工具";
    return {
      kind: "status",
      id: `${conv.conversationId}-tool-running`,
      title: "工具执行中",
      detail: `正在调用 ${toolName}。`,
      timestamp,
    };
  }

  return {
    kind: "status",
    id: `${conv.conversationId}-running`,
    title: "正在处理",
    detail: "请求已发送，正在等待事件流返回。",
    timestamp,
  };
}
