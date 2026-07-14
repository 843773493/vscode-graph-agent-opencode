import type { TimelineItem } from "../timelineTypes";
import { keyFlowSkillNames } from "../skillKeyFlow";
import { formatToolDetail, getOptionalString } from "../tracePayload";

export interface PendingToolCall {
  partId: string;
  itemIndex: number;
  payload: Record<string, unknown>;
  timestamp: string | null;
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

export function buildActiveToolItem(
  pending: PendingToolCall,
): Extract<TimelineItem, { kind: "aggregated_tool" }> {
  return {
    kind: "aggregated_tool",
    id: pending.partId,
    toolName: getOptionalString(pending.payload, "tool_name") || "工具",
    inputText: traceToolInputText(pending.payload),
    resultText: "",
    timestamp: pending.timestamp,
    rawStart: pending.payload,
    rawEnd: {},
    active: true,
  };
}

export function buildCompletedToolItem(
  pending: PendingToolCall,
  resultPayload: Record<string, unknown>,
): Extract<TimelineItem, { kind: "aggregated_tool" }> {
  const resultText = formatToolDetail(getOptionalString(resultPayload, "result"));
  const failed =
    resultPayload.failed === true ||
    getOptionalString(resultPayload, "status").toLowerCase() === "error" ||
    /^error:/i.test(resultText.trim());
  return {
    ...buildActiveToolItem(pending),
    resultText,
    rawEnd: resultPayload,
    active: false,
    failed,
  };
}

export function buildFailedToolItem(
  pending: PendingToolCall,
  failurePayload: Record<string, unknown>,
  eventType: string,
): Extract<TimelineItem, { kind: "aggregated_tool" }> {
  const resultText = formatToolDetail(
    traceErrorSignature(failurePayload) || "工具调用未完成，任务已失败。",
  );
  return {
    ...buildActiveToolItem(pending),
    resultText,
    rawEnd: {
      ...failurePayload,
      event_type: eventType,
      result: resultText,
      skill_names: keyFlowSkillNames(pending.payload.skill_names),
    },
    active: false,
    failed: true,
  };
}
