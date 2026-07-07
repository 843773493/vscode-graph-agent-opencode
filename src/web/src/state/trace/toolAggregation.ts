import type { TimelineItem } from "../timelineTypes";
import { keyFlowSkillNames } from "../skillKeyFlow";
import {
  formatToolDetail,
  getOptionalString,
} from "../tracePayload";

export interface PendingToolCall {
  payload: Record<string, unknown>;
  timestamp: string | null;
  eventId: string | null;
}

export function traceToolEventKey(
  payload: Record<string, unknown>,
  toolName: string,
): string {
  return getOptionalString(payload, "tool_call_run_id") || toolName;
}

export function traceErrorSignature(payload: Record<string, unknown>): string {
  return [
    getOptionalString(payload, "error"),
    getOptionalString(payload, "message"),
    getOptionalString(payload, "detail"),
  ].filter(Boolean).join("\n");
}

export function traceToolArgs(payload: Record<string, unknown>): Record<string, unknown> {
  return typeof payload.args === "object" &&
    payload.args !== null &&
    !Array.isArray(payload.args)
    ? payload.args as Record<string, unknown>
    : {};
}

function traceToolInputText(payload: Record<string, unknown>): string {
  return formatToolDetail(getOptionalString(payload, "message")) ||
    formatToolDetail(getOptionalString(payload, "content")) ||
    formatToolDetail(payload.args);
}

export function buildCompletedToolItem({
  convId,
  pending,
  toolName,
  resultPayload,
  fallbackIndex,
}: {
  convId: string;
  pending: PendingToolCall;
  toolName: string;
  resultPayload: Record<string, unknown>;
  fallbackIndex: number;
}): Extract<TimelineItem, { kind: "aggregated_tool" }> {
  return {
    kind: "aggregated_tool",
    id: `agg-tool-${convId}-${pending.eventId || pending.timestamp || fallbackIndex}`,
    toolName,
    inputText: traceToolInputText(pending.payload),
    resultText: formatToolDetail(getOptionalString(resultPayload, "result")),
    timestamp: pending.timestamp,
    rawStart: pending.payload,
    rawEnd: resultPayload,
  };
}

export function buildFailedToolItems({
  convId,
  pendingToolCalls,
  failurePayload,
  eventType,
  fallbackIndex,
}: {
  convId: string;
  pendingToolCalls: Map<string, PendingToolCall>;
  failurePayload: Record<string, unknown>;
  eventType: string;
  fallbackIndex: number;
}): Array<Extract<TimelineItem, { kind: "aggregated_tool" }>> {
  const resultText = formatToolDetail(
    traceErrorSignature(failurePayload) || "工具调用未完成，任务已失败。",
  );
  return Array.from(pendingToolCalls.values()).map((pending, index) => {
    const toolName = getOptionalString(pending.payload, "tool_name") || "工具";
    const skillNames = keyFlowSkillNames(pending.payload.skill_names);
    return {
      kind: "aggregated_tool",
      id: `agg-tool-failed-${convId}-${pending.eventId || pending.timestamp || fallbackIndex + index}`,
      toolName,
      inputText: traceToolInputText(pending.payload),
      resultText,
      timestamp: pending.timestamp,
      rawStart: pending.payload,
      rawEnd: {
        ...failurePayload,
        event_type: eventType,
        result: resultText,
        skill_names: skillNames,
      },
      failed: true,
    };
  });
}
