import { normalizeDisplayText } from "../utils/displayText";
import { isRecord, redactLargeData } from "../utils/jsonDisplay";
import { allowedToolsFromSkillMarkdownText } from "../utils/skillMarkdown";
import {
  compactKeyFlowText,
  createSkillKeyFlowState,
  recordFinalText,
  recordReadSkill,
  skillKeyFlowSnapshot,
} from "./skillKeyFlow";
import {
  CUSTOM_TOOL_INVOKER_NAME,
  INVALID_CUSTOM_TOOL_CALL_NAME,
  UNKNOWN_CUSTOM_TOOL_NAME,
  customToolCallArgs,
  customToolCallId,
  customToolCallName,
  customToolTargetNameFromCall,
} from "./customTools/protocol";

export interface AgentStateSummary {
  skills: string[];
  customTools: string[];
  customToolResults: Array<{ toolName: string; invocationToolName: string; resultText: string }>;
  finalText: string;
}

export function formatAgentStateJsonlForDisplay(jsonl: string): string {
  return jsonl
    .trim()
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((line) => {
      const parsed: unknown = JSON.parse(line);
      return JSON.stringify(redactLargeData(parsed));
    })
    .join("\n");
}

export function parseAgentStateRecords(jsonl: string): Record<string, unknown>[] {
  return jsonl
    .trim()
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line) as unknown)
    .filter(isRecord);
}

function textContent(value: unknown): string {
  if (typeof value === "string") {
    return normalizeDisplayText(value);
  }
  if (!Array.isArray(value)) {
    return "";
  }
  return value
    .map((item) => {
      if (typeof item === "string") {
        return item;
      }
      if (!isRecord(item)) {
        return "";
      }
      const type = item.type;
      if (type === "text" && typeof item.text === "string") {
        return normalizeDisplayText(item.text);
      }
      return "";
    })
    .join("");
}

function toolCalls(record: Record<string, unknown>): Record<string, unknown>[] {
  const calls = record.tool_calls;
  if (!Array.isArray(calls)) {
    return [];
  }
  return calls.filter(isRecord);
}

function isCustomInvokerValidationError(resultText: string): boolean {
  return (
    resultText.includes("Error invoking tool 'invoke_custom_tool'") &&
    resultText.includes("tool_name: Field required")
  );
}

export function buildAgentStateSummary(
  records: Record<string, unknown>[],
): AgentStateSummary {
  const state = createSkillKeyFlowState();
  const customTools = new Set<string>();
  const customToolResults: Array<{
    toolName: string;
    invocationToolName: string;
    resultText: string;
  }> = [];
  const customToolTargetsByCallId = new Map<string, string>();
  const seenCustomToolResults = new Set<string>();

  for (const record of records) {
    for (const call of toolCalls(record)) {
      const name = customToolCallName(call);
      if (name === "read_file") {
        recordReadSkill(state, { toolName: name, args: customToolCallArgs(call) });
      }
      const customToolName = customToolTargetNameFromCall(call);
      if (customToolName) {
        customTools.add(customToolName);
        const callId = customToolCallId(call);
        if (callId) {
          customToolTargetsByCallId.set(callId, customToolName);
        }
      }
    }

    if (record.role === "tool" && record.name === "read_file") {
      for (const toolName of allowedToolsFromSkillMarkdownText(textContent(record.content))) {
        customTools.add(toolName);
      }
    }
  }

  for (const record of records) {
    if (record.role === "tool") {
      const toolName = typeof record.name === "string" ? record.name : "";
      if (toolName === CUSTOM_TOOL_INVOKER_NAME) {
        const callId = customToolCallId(record);
        const customToolName = callId
          ? customToolTargetsByCallId.get(callId)
          : "";
        const resultText = compactKeyFlowText(textContent(record.content));
        const displayToolName = customToolName ||
          (isCustomInvokerValidationError(resultText)
            ? INVALID_CUSTOM_TOOL_CALL_NAME
            : UNKNOWN_CUSTOM_TOOL_NAME);
        if (resultText) {
          const resultKey = `${callId || "missing-id"}\u0000${displayToolName}\u0000${resultText}`;
          if (seenCustomToolResults.has(resultKey)) {
            continue;
          }
          seenCustomToolResults.add(resultKey);
          customToolResults.push({
            toolName: displayToolName,
            invocationToolName: CUSTOM_TOOL_INVOKER_NAME,
            resultText,
          });
        }
      }
    }

    if (record.role === "assistant") {
      const metadata = isRecord(record.response_metadata)
        ? record.response_metadata
        : {};
      if (metadata.phase === "final_answer") {
        const text = textContent(record.content).trim();
        if (text) {
          recordFinalText(state, text, Number.MAX_SAFE_INTEGER);
        }
      }
    }
  }
  const snapshot = skillKeyFlowSnapshot(state);

  return {
    skills: snapshot.readSkills,
    customTools: Array.from(customTools),
    customToolResults,
    finalText: snapshot.finalText,
  };
}
