import type { TraceEvent } from "../types/backend";
import type { ConversationView } from "../types/frontend";
import type { TimelineItem } from "./timelineTypes";
import {
  extractEventInfo,
  formatToolDetail,
  getOptionalString,
} from "./tracePayload";

/** 按顺序处理事件，将 text_start/delta/end 合并为 aggregated_text，将 tool_call_start/end 合并为 aggregated_tool。 */
export function aggregateConversationEvents(
  events: TraceEvent[],
  convId: string,
  isRunning: boolean,
): TimelineItem[] {
  const items: TimelineItem[] = [];
  const seenIds = new Set<string>();
  let hasOutputContent = false;

  let textBuf: string[] = [];
  let textPhase = "";
  let textTs: string | null = null;
  let textEventCount = 0;
  let textRawEvents: Array<{ type: string; payload: Record<string, unknown> }> =
    [];
  let textFirstId: string | null = null;

  const pendingToolCalls = new Map<
    string,
    {
      payload: Record<string, unknown>;
      timestamp: string | null;
      eventId: string | null;
    }
  >();

  let llmModel = "";
  let agentEndTs: string | null = null;
  let sawSuccessfulTerminalEvent = false;
  let sawSessionInterrupted = false;

  function flushTextBuf(active: boolean) {
    if (textBuf.length === 0) return;
    const allText = textBuf.join("");
    if (!allText.trim()) {
      textBuf = [];
      textPhase = "";
      textTs = null;
      textEventCount = 0;
      textRawEvents = [];
      textFirstId = null;
      return;
    }
    hasOutputContent = true;
    items.push({
      kind: "aggregated_text",
      id: `agg-text-${convId}-${textFirstId || textTs || items.length}`,
      text: allText,
      phase: textPhase,
      active,
      timestamp: textTs,
      eventCount: textEventCount,
      rawEvents: textRawEvents,
    });
    textBuf = [];
    textPhase = "";
    textTs = null;
    textEventCount = 0;
    textRawEvents = [];
    textFirstId = null;
  }

  function addTraceItem(
    eventType: string,
    payload: Record<string, unknown>,
    timestamp: string | null,
    eventId: string | null,
  ) {
    const uniqueKey = eventId
      ? `trace-${eventId}`
      : `trace-${eventType}-${timestamp ?? items.length}`;
    if (eventId && seenIds.has(uniqueKey)) return;
    if (eventId) seenIds.add(uniqueKey);
    items.push({ kind: "trace", id: uniqueKey, eventType, payload, timestamp });
  }

  for (const event of events) {
    const { eventType, payload, timestamp, eventId } = extractEventInfo(event);
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
      flushTextBuf(false);
      const initialText = getOptionalString(payload, "text");
      textBuf = initialText ? [initialText] : [];
      const startPhase =
        getOptionalString(payload, "phase") ||
        getOptionalString(payload, "kind");
      textPhase =
        startPhase === "reasoning" ? "reasoning" : startPhase ? "text" : "";
      textTs = timestamp;
      textEventCount = initialText ? 1 : 0;
      textRawEvents = initialText ? [{ type: eventType, payload }] : [];
      textFirstId = eventId;
      continue;
    }
    if (type === "text_delta") {
      const deltaKind = getOptionalString(payload, "kind") || "text";
      const deltaPhase = deltaKind === "reasoning" ? "reasoning" : "text";
      const deltaText = getOptionalString(payload, "text");
      if (textPhase && textPhase !== deltaPhase) {
        flushTextBuf(false);
      }
      if (deltaText) {
        textBuf.push(deltaText);
        textEventCount++;
        textRawEvents.push({ type: eventType, payload });
      }
      textPhase = deltaPhase;
      if (!textTs) textTs = timestamp;
      if (!textFirstId) textFirstId = eventId;
      continue;
    }
    if (type === "text_end") {
      const finalText = getOptionalString(payload, "text");
      if (finalText) {
        textBuf = [finalText];
        textPhase = "text";
        textTs = textTs || timestamp;
        textEventCount = 1;
        textRawEvents = [{ type: eventType, payload }];
        textFirstId = textFirstId || eventId;
      }
      flushTextBuf(false);
      continue;
    }

    if (type === "tool_call_start") {
      flushTextBuf(false);
      const toolName = getOptionalString(payload, "tool_name");
      pendingToolCalls.set(toolName, { payload, timestamp, eventId });
      continue;
    }
    if (type === "tool_call_end") {
      flushTextBuf(false);
      const toolName = getOptionalString(payload, "tool_name");
      const pending = pendingToolCalls.get(toolName);
      if (pending) {
        pendingToolCalls.delete(toolName);
        hasOutputContent = true;
        const startPayload = pending.payload;
        const resultText = formatToolDetail(getOptionalString(payload, "result"));
        const inputMsg =
          formatToolDetail(getOptionalString(startPayload, "message")) ||
          formatToolDetail(getOptionalString(startPayload, "content")) ||
          formatToolDetail(startPayload.args);
        items.push({
          kind: "aggregated_tool",
          id: `agg-tool-${convId}-${pending.eventId || pending.timestamp || items.length}`,
          toolName,
          inputText: inputMsg,
          resultText,
          timestamp: pending.timestamp,
          rawStart: startPayload,
          rawEnd: payload,
        });
      } else {
        addTraceItem(eventType, payload, timestamp, eventId);
      }
      continue;
    }

    if (type === "session_interrupted") {
      flushTextBuf(false);
      hasOutputContent = true;
      sawSessionInterrupted = true;
      addTraceItem(eventType, payload, timestamp, eventId);
      continue;
    }

    if (
      sawSessionInterrupted &&
      ((type === "error" &&
        getOptionalString(payload, "error") === "任务被取消") ||
        type === "job_cancelled")
    ) {
      continue;
    }

    if (
      type === "error" ||
      type === "job_failed" ||
      type === "job_cancelled"
    ) {
      flushTextBuf(false);
      hasOutputContent = true;
      addTraceItem(eventType, payload, timestamp, eventId);
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
      type === "agent_end"
    ) {
      continue;
    }

    if (type === "message_created") {
      continue;
    }

    flushTextBuf(false);
    hasOutputContent = true;
    addTraceItem(eventType, payload, timestamp, eventId);
  }

  flushTextBuf(isRunning);
  for (const [, pending] of pendingToolCalls) {
    addTraceItem(
      "tool_call_start",
      pending.payload,
      pending.timestamp,
      pending.eventId,
    );
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

  return items;
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
