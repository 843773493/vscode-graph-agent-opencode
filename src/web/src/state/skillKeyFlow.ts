import { normalizeDisplayText } from "../utils/displayText";
import { skillNameFromPath } from "../utils/skillPaths";

export interface SkillToolCallSummary {
  toolName: string;
  skillNames: string[];
}

export interface SkillToolResultSummary extends SkillToolCallSummary {
  resultText: string;
}

export interface SkillKeyFlowState {
  readSkills: Set<string>;
  skillToolCalls: Map<string, SkillToolCallSummary>;
  skillToolResults: Map<string, SkillToolResultSummary>;
  finalText: string;
}

export interface SkillKeyFlowSnapshot {
  readSkills: string[];
  skillToolCalls: SkillToolCallSummary[];
  skillToolResults: SkillToolResultSummary[];
  finalText: string;
}

export function createSkillKeyFlowState(): SkillKeyFlowState {
  return {
    readSkills: new Set<string>(),
    skillToolCalls: new Map<string, SkillToolCallSummary>(),
    skillToolResults: new Map<string, SkillToolResultSummary>(),
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
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
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

export function recordSkillToolCall(
  state: SkillKeyFlowState,
  options: { toolName: string; skillNames: string[] },
): void {
  const { toolName, skillNames } = options;
  if (!toolName || skillNames.length === 0) {
    return;
  }
  state.skillToolCalls.set(toolName, { toolName, skillNames });
}

export function recordSkillToolResult(
  state: SkillKeyFlowState,
  options: { toolName: string; skillNames: string[]; resultText: string },
): void {
  const { toolName, skillNames, resultText } = options;
  const startCall = toolName ? state.skillToolCalls.get(toolName) : undefined;
  const effectiveSkillNames = skillNames.length > 0 ? skillNames : (startCall?.skillNames ?? []);
  if (!toolName || effectiveSkillNames.length === 0 || !resultText) {
    return;
  }
  state.skillToolResults.set(toolName, {
    toolName,
    skillNames: effectiveSkillNames,
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
    skillToolCalls: Array.from(state.skillToolCalls.values()),
    skillToolResults: Array.from(state.skillToolResults.values()),
    finalText: state.finalText,
  };
}

export function hiddenToolNameFromInitialTools(
  toolName: string,
  initialToolNames: Set<string>,
): string {
  return toolName && toolName !== "read_file" && !initialToolNames.has(toolName) ? toolName : "";
}
