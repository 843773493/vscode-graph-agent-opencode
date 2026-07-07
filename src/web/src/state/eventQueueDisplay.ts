import type { FrontendReceivedEvent } from "../types/frontend";
import {
  compactKeyFlowText,
  createSkillKeyFlowState,
  keyFlowSkillNames,
  recordFinalText,
  recordReadSkill,
  skillKeyFlowSnapshot,
} from "./skillKeyFlow";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function eventPayload(item: FrontendReceivedEvent): Record<string, unknown> {
  if (item.kind === "frontend") {
    return item.payload ?? {};
  }
  const payload = item.event.raw?.payload ?? item.event.payload ?? {};
  if (
    Array.isArray(item.event.skill_names) &&
    item.event.skill_names.length > 0 &&
    !Array.isArray(payload.skill_names)
  ) {
    return { ...payload, skill_names: item.event.skill_names };
  }
  return payload;
}

export function eventType(item: FrontendReceivedEvent): string {
  return item.kind === "frontend" ? item.type : item.event.type;
}

export function eventTimestamp(item: FrontendReceivedEvent): string {
  return item.kind === "frontend" ? item.receivedAt : item.event.timestamp;
}

export function eventSessionId(item: FrontendReceivedEvent): string {
  return item.sessionId;
}

export type EventQueueDisplayItem =
  | { kind: "event"; item: FrontendReceivedEvent }
  | { kind: "text_delta_group"; items: FrontendReceivedEvent[] };

function uniqueTraceItems(items: FrontendReceivedEvent[]): FrontendReceivedEvent[] {
  const seen = new Set<string>();
  const result: FrontendReceivedEvent[] = [];
  for (const item of items) {
    if (item.kind !== "trace") {
      continue;
    }
    const key = item.event.event_id || item.id;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    result.push(item);
  }
  return result;
}

function eventSortTime(item: FrontendReceivedEvent): number {
  const parsedEventTime = Date.parse(eventTimestamp(item));
  if (Number.isFinite(parsedEventTime)) {
    return parsedEventTime;
  }
  const parsedReceivedTime = Date.parse(item.receivedAt);
  return Number.isFinite(parsedReceivedTime) ? parsedReceivedTime : 0;
}

export function orderedTraceItems(items: FrontendReceivedEvent[]): FrontendReceivedEvent[] {
  return uniqueTraceItems(items)
    .map((item, index) => ({ item, index }))
    .sort((left, right) => {
      const timeDelta = eventSortTime(left.item) - eventSortTime(right.item);
      if (timeDelta !== 0) {
        return timeDelta;
      }
      return left.index - right.index;
    })
    .map(({ item }) => item);
}

function textDeltaGroupKey(item: FrontendReceivedEvent): string {
  if (item.kind !== "trace" || item.event.type !== "text_delta") {
    return "";
  }

  const event = item.event;
  const payload = eventPayload(item);
  return [
    item.sessionId,
    item.source,
    event.job_id,
    event.step_id ?? "",
    event.agent_id ?? "",
    event.phase ?? "",
    payload.kind ?? "",
    payload.phase ?? "",
  ].join("|");
}

export function buildDisplayItems(items: FrontendReceivedEvent[]): EventQueueDisplayItem[] {
  const result: EventQueueDisplayItem[] = [];
  let pendingTextDeltas: FrontendReceivedEvent[] = [];
  let pendingKey = "";

  const flushPending = () => {
    if (pendingTextDeltas.length === 0) {
      return;
    }
    if (pendingTextDeltas.length === 1) {
      result.push({ kind: "event", item: pendingTextDeltas[0] });
    } else {
      result.push({ kind: "text_delta_group", items: pendingTextDeltas });
    }
    pendingTextDeltas = [];
    pendingKey = "";
  };

  for (const item of items) {
    const key = textDeltaGroupKey(item);
    if (key) {
      if (pendingTextDeltas.length > 0 && pendingKey !== key) {
        flushPending();
      }
      pendingTextDeltas.push(item);
      pendingKey = key;
      continue;
    }

    flushPending();
    result.push({ kind: "event", item });
  }

  flushPending();
  return result;
}

export function textDeltaText(item: FrontendReceivedEvent): string {
  const payload = eventPayload(item);
  const text = payload.text;
  return typeof text === "string" ? text : "";
}

export function textDeltaKind(item: FrontendReceivedEvent): string {
  const payload = eventPayload(item);
  const kind = payload.kind ?? payload.phase;
  return typeof kind === "string" ? kind : "";
}

export function attachmentNames(payload: Record<string, unknown>): string[] {
  const attachments = payload.attachments;
  if (!Array.isArray(attachments)) {
    return [];
  }
  return attachments.flatMap((attachment) => {
    if (!isRecord(attachment)) {
      return [];
    }
    const name = attachment.name ?? attachment.file_id;
    return typeof name === "string" && name ? [name] : [];
  });
}

export interface EventQueueKeyTraceSummary {
  readSkills: string[];
  keyFlowToolCalls: Array<{ toolName: string; skillNames: string[]; invocationToolName?: string }>;
  keyFlowToolResults: Array<{
    toolName: string;
    skillNames: string[];
    resultText: string;
    invocationToolName?: string;
  }>;
  finalText: string;
}

function payloadString(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function traceToolLabel(item: { toolName: string; invocationToolName?: string }): string {
  return item.invocationToolName
    ? `${item.invocationToolName} -> ${item.toolName}`
    : item.toolName;
}

export function buildKeyTraceSummary(
  items: FrontendReceivedEvent[],
): EventQueueKeyTraceSummary {
  const state = createSkillKeyFlowState();
  const toolCalls: EventQueueKeyTraceSummary["keyFlowToolCalls"] = [];
  const toolResults: EventQueueKeyTraceSummary["keyFlowToolResults"] = [];
  const seenCalls = new Set<string>();
  const seenResults = new Set<string>();

  for (const item of items) {
    const type = eventType(item);
    const payload = eventPayload(item);
    if (type === "tool_call_start") {
      const toolName = payloadString(payload, "tool_name");
      if (toolName === "read_file" && isRecord(payload.args)) {
        recordReadSkill(state, { toolName, args: payload.args });
      }
      const skillNames = keyFlowSkillNames(payload.skill_names);
      const invocationToolName = payloadString(payload, "invocation_tool_name");
      const shouldSummarizeTool = skillNames.length > 0 || invocationToolName;
      const key = `${invocationToolName}\u0000${toolName}\u0000${skillNames.join("\u0000")}`;
      if (toolName && shouldSummarizeTool && !seenCalls.has(key)) {
        seenCalls.add(key);
        toolCalls.push({ toolName, skillNames, invocationToolName });
      }
    }

    if (type === "tool_call_end") {
      const toolName = payloadString(payload, "tool_name");
      const skillNames = keyFlowSkillNames(payload.skill_names);
      const invocationToolName = payloadString(payload, "invocation_tool_name");
      const resultText = compactKeyFlowText(payload.result, 120);
      const shouldSummarizeTool = skillNames.length > 0 || invocationToolName;
      const key = `${invocationToolName}\u0000${toolName}\u0000${skillNames.join("\u0000")}\u0000${resultText}`;
      if (toolName && resultText && shouldSummarizeTool && !seenResults.has(key)) {
        seenResults.add(key);
        toolResults.push({ toolName, skillNames, resultText, invocationToolName });
      }
    }

    if (type === "text_end" && typeof payload.text === "string" && payload.text.trim()) {
      recordFinalText(state, payload.text);
    }
  }
  const snapshot = skillKeyFlowSnapshot(state);

  return {
    readSkills: snapshot.readSkills,
    keyFlowToolCalls: toolCalls.length > 0 ? toolCalls : snapshot.keyFlowToolCalls,
    keyFlowToolResults: toolResults.length > 0 ? toolResults : snapshot.keyFlowToolResults,
    finalText: snapshot.finalText,
  };
}

export function toolEventSummary(
  type: string,
  payload: Record<string, unknown>,
): { toolName: string; displayToolName: string; summary: string; skillNames: string[] } | null {
  if (type !== "tool_call_start" && type !== "tool_call_end") {
    return null;
  }
  const toolName = typeof payload.tool_name === "string" ? payload.tool_name : "";
  if (!toolName) {
    return null;
  }
  const skillNames = keyFlowSkillNames(payload.skill_names);
  const invocationToolName = payloadString(payload, "invocation_tool_name");
  const displayToolName = traceToolLabel({ toolName, invocationToolName });
  if (type === "tool_call_start") {
    return {
      toolName,
      displayToolName,
      skillNames,
      summary: compactKeyFlowText(payload.args, 180) || "无参数",
    };
  }
  return {
    toolName,
    displayToolName,
    skillNames,
    summary: compactKeyFlowText(payload.result, 180) || "无返回内容",
  };
}
