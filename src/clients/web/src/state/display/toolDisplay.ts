import { t } from "../../../../shared/core/i18n/index.js";
import type { TimelineItem } from "../timeline/timelineTypes";
import { isRecord } from "../../utils/jsonDisplay";
import { truncateDisplayText } from "../../utils/displayText";

type AggregatedToolItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;

const ROUTINE_INTERNAL_TOOL_NAMES = new Set([
  "glob",
  "grep",
  "ls",
  "read_file",
]);

function parseJsonRecord(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) {
    return value;
  }
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed.startsWith("{")) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  return isRecord(parsed) ? parsed : null;
}

function toolInputRecord(item: AggregatedToolItem): Record<string, unknown> {
  return parseJsonRecord(item.inputText) ??
    parseJsonRecord(item.rawStart.args) ??
    {};
}

function toolResultRecord(item: AggregatedToolItem): Record<string, unknown> {
  return parseJsonRecord(item.resultText) ??
    parseJsonRecord(item.rawEnd.result) ??
    {};
}

function fieldText(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (value === null || value === undefined || value === "") {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function fieldTextList(record: Record<string, unknown>, key: string): string[] {
  const value = record[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

function markdownCode(value: string): string {
  return `\`${value.replace(/`/g, "\\`")}\``;
}

function toolFieldLines(rows: Array<[label: string, value: string]>): string {
  return rows
    .filter(([, value]) => value.trim().length > 0)
    .map(([label, value]) => `- ${label}：${value}`)
    .join("\n");
}

function commandStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    completed: t("chat.tool.commandCompleted"),
    background: t("chat.tool.commandBackground"),
    running: t("chat.tool.commandRunning"),
    terminated: t("chat.tool.commandTerminated"),
    deleted: t("chat.tool.commandDeleted"),
    failed: t("chat.tool.commandFailed"),
  };
  return labels[status] ?? status;
}

function compactInline(value: string, maxLength = 120): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${truncateDisplayText(text, maxLength)}...`;
}

function isTerminalToolItem(item: AggregatedToolItem): boolean {
  return item.toolName === "exec_command" || item.toolName === "write_stdin";
}

function formatTerminalToolContent(
  inputRecord: Record<string, unknown>,
  resultRecord: Record<string, unknown>,
): ToolDetails {
  const terminalId = fieldText(resultRecord, "chunk_id") ||
    fieldText(resultRecord, "session_id") ||
    fieldText(inputRecord, "session_id");
  const runningSessionId = fieldText(resultRecord, "session_id");
  const exitCode = fieldText(resultRecord, "exit_code");
  const hasExitCode = Object.prototype.hasOwnProperty.call(resultRecord, "exit_code");
  const status = runningSessionId ? "running" : hasExitCode ? "completed" : "";
  const output = fieldText(resultRecord, "output");

  const input = [
    t("chat.tool.inputHeading"),
    toolFieldLines([
      [t("chat.tool.command"), fieldText(inputRecord, "cmd")],
      ["Session ID", fieldText(inputRecord, "session_id")],
      [t("chat.tool.characters"), fieldText(inputRecord, "chars")],
      [t("chat.tool.wait"), fieldText(inputRecord, "yield_time_ms")],
      [t("chat.tool.outputLimit"), fieldText(inputRecord, "max_output_tokens")],
      [t("chat.tool.cwd"), fieldText(inputRecord, "workdir")],
      ["Shell", fieldText(inputRecord, "shell")],
      ["PTY", fieldText(inputRecord, "tty")],
      ["Login shell", fieldText(inputRecord, "login")],
    ]),
  ].filter(Boolean).join("\n\n");
  const result = [
    t("chat.tool.resultHeading"),
    toolFieldLines([
      [t("chat.tool.commandStatus"), status ? commandStatusLabel(status) : ""],
      ["Session ID", runningSessionId ? markdownCode(runningSessionId) : ""],
      [t("chat.tool.terminalId"), terminalId ? markdownCode(terminalId) : ""],
      [t("chat.tool.exitCode"), exitCode],
      ["Chunk ID", fieldText(resultRecord, "chunk_id")],
      [t("chat.tool.originalTokens"), fieldText(resultRecord, "original_token_count")],
      [t("chat.tool.duration"), fieldText(resultRecord, "wall_time_seconds")],
    ]),
    terminalId
      ? t("chat.tool.terminalHint")
      : "",
    output ? t("chat.tool.outputContent", output) : "",
  ].filter((part) => part.trim().length > 0).join("\n\n");

  return { input, result };
}

function terminalToolCollapsedText(
  item: AggregatedToolItem,
  resultRecord: Record<string, unknown>,
): string {
  if (fieldText(resultRecord, "session_id")) {
    return item.toolName === "write_stdin"
      ? t("chat.tool.writtenRunning")
      : t("chat.tool.runningAttach");
  }
  if (Object.prototype.hasOwnProperty.call(resultRecord, "exit_code")) {
    return t("chat.tool.completedOpen");
  }
  return t("chat.tool.returnedOpen");
}

function skillNames(item: AggregatedToolItem): string[] {
  return fieldTextList(item.rawStart, "skill_names");
}

function invocationToolName(item: AggregatedToolItem): string {
  return fieldText(item.rawStart, "invocation_tool_name") ||
    fieldText(item.rawEnd, "invocation_tool_name");
}

function invocationLabel(item: AggregatedToolItem): string {
  const invocation = invocationToolName(item);
  return invocation ? `${invocation} -> ${item.toolName}` : item.toolName;
}

export function isSkillInternalToolItem(item: AggregatedToolItem): boolean {
  return item.toolName === "skill_load" || skillNames(item).length > 0;
}

export function isRoutineInternalToolItem(item: AggregatedToolItem): boolean {
  return ROUTINE_INTERNAL_TOOL_NAMES.has(item.toolName);
}

interface ToolDetails { input: string; result: string }

function formatCustomToolCardContent(item: AggregatedToolItem): ToolDetails {
  const names = skillNames(item);
  const invocation = invocationToolName(item);
  return {
    input: [
      names.length ? t("chat.tool.skillContent", names.map(markdownCode).join(t("chat.nameSeparator"))) : "",
      invocation ? t("chat.tool.invocationContent", markdownCode(invocation), markdownCode(item.toolName)) : "",
      item.inputText ? t("chat.tool.inputBody", item.inputText) : "",
    ].filter(Boolean).join("\n\n"),
    result: item.resultText ? t("chat.tool.outputBody", item.resultText) : "",
  };
}

export interface ToolRowDisplay {
  previewText: string;
  detailsContent: ToolDetails | null;
}

export function deriveToolRowDisplay(
  item: AggregatedToolItem,
  includeExpandedContent: boolean,
): ToolRowDisplay {
  if (isTerminalToolItem(item)) {
    const resultRecord = toolResultRecord(item);
    const previewText = item.declared ? t("chat.tool.executionUnavailable")
      : item.active ? t("chat.tool.running", item.toolName) : terminalToolCollapsedText(item, resultRecord);
    return {
      previewText,
      detailsContent: includeExpandedContent
        ? formatTerminalToolContent(toolInputRecord(item), resultRecord)
        : null,
    };
  }
  if (item.outcomeUnknown) {
    return {
      previewText: t("chat.tool.unconfirmed"),
      detailsContent: includeExpandedContent
        ? formatCustomToolCardContent(item)
        : null,
    };
  }

  const inputRecord = toolInputRecord(item);
  const resultRecord = toolResultRecord(item);
  const path = fieldText(inputRecord, "file_path") || fieldText(inputRecord, "path");
  const names = skillNames(item);
  const prefix = names.length > 0
    ? t("chat.tool.recorded", names.join(t("chat.nameSeparator")), invocationLabel(item))
    : invocationLabel(item);
  const result =
    fieldText(resultRecord, "stdout") ||
    fieldText(resultRecord, "result") ||
    fieldText(resultRecord, "content") ||
    item.resultText;
  return {
    previewText: item.declared ? t("chat.tool.executionUnavailable") : result.trim()
      ? t("chat.tool.returned", prefix, compactInline(result, 80))
      : path.trim()
        ? t("chat.tool.processing", prefix, compactInline(path, 80))
        : t("chat.tool.done", prefix),
    detailsContent: includeExpandedContent
      ? formatCustomToolCardContent(item)
      : null,
  };
}
