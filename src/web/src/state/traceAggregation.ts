import type { TraceEvent } from "../types/backend";
import type { ConversationView } from "../types/frontend";
import {
  createSkillKeyFlowState,
  keyFlowSkillNames,
  recordFinalText,
  recordKeyFlowToolCall,
  recordKeyFlowToolResult,
  recordReadSkill,
  skillKeyFlowSnapshot,
} from "./skillKeyFlow";
import type { TimelineItem } from "./timelineTypes";
import {
  extractEventInfo,
  getOptionalString,
} from "./tracePayload";
import {
  buildCompletedToolItem,
  buildFailedToolItems,
  traceErrorSignature,
  traceToolArgs,
  traceToolEventKey,
  type PendingToolCall,
} from "./trace/toolAggregation";

/** 按顺序处理事件，将 text_start/delta/end 合并为 aggregated_text，将 tool_call_start/end 合并为 aggregated_tool。 */
export function aggregateConversationEvents(
  events: TraceEvent[],
  convId: string,
  isRunning: boolean,
): TimelineItem[] {
  const items: TimelineItem[] = [];
  const seenIds = new Set<string>();
  let hasOutputContent = false;
  const skillFlow = createSkillKeyFlowState();

  let textBuf: string[] = [];
  let textPhase = "";
  let textTs: string | null = null;
  let textEventCount = 0;
  let textRawEvents: Array<{ type: string; payload: Record<string, unknown> }> =
    [];
  let textFirstId: string | null = null;

  const pendingToolCalls = new Map<string, PendingToolCall>();

  let llmModel = "";
  let agentEndTs: string | null = null;
  let sawSuccessfulTerminalEvent = false;
  let sawSessionInterrupted = false;
  let lastErrorSignature = "";

  function flushPendingToolsAsFailed(
    payload: Record<string, unknown>,
    eventType: string,
  ) {
    items.push(...buildFailedToolItems({
      convId,
      pendingToolCalls,
      failurePayload: payload,
      eventType,
      fallbackIndex: items.length,
    }));
    pendingToolCalls.clear();
  }

  function flushTextBuf(active: boolean, phaseOverride?: string) {
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
      phase: phaseOverride ?? textPhase,
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
      recordFinalText(skillFlow, finalText);
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
      flushTextBuf(false, textPhase === "text" ? "reasoning" : undefined);
      const toolName = getOptionalString(payload, "tool_name");
      const args = traceToolArgs(payload);
      recordReadSkill(skillFlow, { toolName, args });
      recordKeyFlowToolCall(skillFlow, {
        toolName,
        skillNames: keyFlowSkillNames(payload.skill_names),
        invocationToolName: getOptionalString(payload, "invocation_tool_name"),
      });
      pendingToolCalls.set(traceToolEventKey(payload, toolName), { payload, timestamp, eventId });
      continue;
    }
    if (type === "tool_call_end") {
      flushTextBuf(false);
      const toolName = getOptionalString(payload, "tool_name");
      const key = traceToolEventKey(payload, toolName);
      const pending = pendingToolCalls.get(key) ?? pendingToolCalls.get(toolName);
      if (pending) {
        pendingToolCalls.delete(key);
        pendingToolCalls.delete(toolName);
        hasOutputContent = true;
        const startPayload = pending.payload;
        const completedToolItem = buildCompletedToolItem({
          convId,
          pending,
          toolName,
          resultPayload: payload,
          fallbackIndex: items.length,
        });
        const skillNames = Array.from(new Set([
          ...keyFlowSkillNames(startPayload.skill_names),
          ...keyFlowSkillNames(payload.skill_names),
        ]));
        recordKeyFlowToolResult(skillFlow, {
          toolName,
          skillNames,
          resultText: completedToolItem.resultText,
          invocationToolName:
            getOptionalString(startPayload, "invocation_tool_name") ||
            getOptionalString(payload, "invocation_tool_name"),
        });
        items.push(completedToolItem);
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
      if (pendingToolCalls.size > 0) {
        flushPendingToolsAsFailed(payload, eventType);
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
      (item) =>
        item.kind === "aggregated_text" &&
        item.phase !== "reasoning" &&
        !item.active,
    );
    if (finalTextIndex === -1) {
      items.push(summaryItem);
    } else {
      items.splice(finalTextIndex, 0, summaryItem);
    }
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
