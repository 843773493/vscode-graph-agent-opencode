import { normalizeDisplayText } from "../utils/displayText";
import { skillNameFromPath } from "../utils/skillPaths";

export interface KeyFlowToolCallSummary {
  toolName: string;
  skillNames: string[];
  invocationToolName?: string;
}

export interface KeyFlowToolResultSummary extends KeyFlowToolCallSummary {
  resultText: string;
}

export interface SkillKeyFlowState {
  readSkills: Set<string>;
  keyFlowToolCalls: Map<string, KeyFlowToolCallSummary>;
  keyFlowToolResults: Map<string, KeyFlowToolResultSummary>;
  finalText: string;
}

export interface SkillKeyFlowSnapshot {
  readSkills: string[];
  keyFlowToolCalls: KeyFlowToolCallSummary[];
  keyFlowToolResults: KeyFlowToolResultSummary[];
  finalText: string;
}

export function createSkillKeyFlowState(): SkillKeyFlowState {
  return {
    readSkills: new Set<string>(),
    keyFlowToolCalls: new Map<string, KeyFlowToolCallSummary>(),
    keyFlowToolResults: new Map<string, KeyFlowToolResultSummary>(),
    finalText: "",
  };
}

export function compactKeyFlowText(value: unknown, maxLength = 120): string {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  const raw = typeof value === "string" ? value : JSON.stringify(value);
  const text = normalizeDisplayText(raw).replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength)}...`;
}

export function keyFlowSkillNames(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return Array.from(new Set(value.filter(
    (item): item is string => typeof item === "string" && item.trim().length > 0,
  )));
}

export function recordReadSkill(
  state: SkillKeyFlowState,
  options: { toolName: string; args: Record<string, unknown> },
): void {
  const { toolName, args } = options;
  if (toolName !== "read_file") {
    return;
  }
  const skillName = skillNameFromPath(args.file_path ?? args.path);
  if (skillName) {
    state.readSkills.add(skillName);
  }
}

export function recordKeyFlowToolCall(
  state: SkillKeyFlowState,
  options: { toolName: string; skillNames: string[]; invocationToolName?: string },
): void {
  const { toolName, skillNames, invocationToolName } = options;
  if (!toolName || skillNames.length === 0) {
    return;
  }
  state.keyFlowToolCalls.set(toolName, { toolName, skillNames, invocationToolName });
}

export function recordKeyFlowToolResult(
  state: SkillKeyFlowState,
  options: {
    toolName: string;
    skillNames: string[];
    resultText: string;
    invocationToolName?: string;
  },
): void {
  const { toolName, skillNames, resultText, invocationToolName } = options;
  const startCall = toolName ? state.keyFlowToolCalls.get(toolName) : undefined;
  const effectiveSkillNames = Array.from(new Set(
    skillNames.length > 0 ? skillNames : (startCall?.skillNames ?? []),
  ));
  if (!toolName || effectiveSkillNames.length === 0 || !resultText) {
    return;
  }
  state.keyFlowToolResults.set(toolName, {
    toolName,
    skillNames: effectiveSkillNames,
    invocationToolName: invocationToolName || startCall?.invocationToolName,
    resultText,
  });
}

export function recordFinalText(
  state: SkillKeyFlowState,
  text: unknown,
  maxLength = 160,
): void {
  const finalText = compactKeyFlowText(text, maxLength);
  if (finalText) {
    state.finalText = finalText;
  }
}

export function skillKeyFlowSnapshot(state: SkillKeyFlowState): SkillKeyFlowSnapshot {
  return {
    readSkills: Array.from(state.readSkills),
    keyFlowToolCalls: Array.from(state.keyFlowToolCalls.values()),
    keyFlowToolResults: Array.from(state.keyFlowToolResults.values()),
    finalText: state.finalText,
  };
}
