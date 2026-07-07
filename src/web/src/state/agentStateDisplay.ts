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

export interface AgentStateSummary {
  skills: string[];
  hiddenTools: string[];
  hiddenToolResults: Array<{ toolName: string; resultText: string }>;
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

export function buildAgentStateSummary(
  records: Record<string, unknown>[],
): AgentStateSummary {
  const state = createSkillKeyFlowState();
  const hiddenTools = new Set<string>();
  const hiddenToolResults = new Map<string, { toolName: string; resultText: string }>();

  for (const record of records) {
    for (const call of toolCalls(record)) {
      const name = typeof call.name === "string" ? call.name : "";
      if (name === "read_file" && isRecord(call.args)) {
        recordReadSkill(state, { toolName: name, args: call.args });
      }
    }

    if (record.role === "tool" && record.name === "read_file") {
      for (const toolName of allowedToolsFromSkillMarkdownText(textContent(record.content))) {
        hiddenTools.add(toolName);
      }
    }
  }

  for (const record of records) {
    for (const call of toolCalls(record)) {
      const name = typeof call.name === "string" ? call.name : "";
      if (name && hiddenTools.has(name)) {
        hiddenTools.add(name);
      }
    }

    if (record.role === "tool") {
      const toolName = typeof record.name === "string" ? record.name : "";
      if (toolName && hiddenTools.has(toolName)) {
        const resultText = compactKeyFlowText(textContent(record.content));
        if (resultText) {
          hiddenToolResults.set(toolName, { toolName, resultText });
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
    hiddenTools: Array.from(hiddenTools),
    hiddenToolResults: Array.from(hiddenToolResults.values()),
    finalText: snapshot.finalText,
  };
}
