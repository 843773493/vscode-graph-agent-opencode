import type { TimelineItem } from "./timelineTypes";
import { isRecord } from "../utils/jsonDisplay";
import { skillNameFromPath } from "../utils/skillPaths";

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
    completed: "命令已完成",
    background: "命令仍在后台运行",
    running: "命令运行中",
    terminated: "命令已终止",
    deleted: "命令记录已删除",
    failed: "命令失败",
  };
  return labels[status] ?? status;
}

function compactInline(value: string, maxLength = 120): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength)}...`;
}

function formatTerminalToolContent(item: AggregatedToolItem): string | null {
  if (item.toolName !== "exec_command" && item.toolName !== "write_stdin") {
    return null;
  }

  const inputRecord = parseJsonRecord(item.rawStart.args) ?? {};
  const resultRecord = parseJsonRecord(item.rawEnd.result) ?? {};
  const terminalId = fieldText(resultRecord, "chunk_id") ||
    fieldText(resultRecord, "session_id") ||
    fieldText(inputRecord, "session_id");
  const runningSessionId = fieldText(resultRecord, "session_id");
  const exitCode = fieldText(resultRecord, "exit_code");
  const status = runningSessionId ? "running" : exitCode ? "completed" : "";
  const output = fieldText(resultRecord, "output");

  const sections = [
    "**输入参数**",
    toolFieldLines([
      ["命令", fieldText(inputRecord, "cmd")],
      ["Session ID", fieldText(inputRecord, "session_id")],
      ["写入字符", fieldText(inputRecord, "chars")],
      ["前台等待", fieldText(inputRecord, "yield_time_ms")],
      ["输出上限", fieldText(inputRecord, "max_output_tokens")],
      ["工作目录", fieldText(inputRecord, "workdir")],
      ["Shell", fieldText(inputRecord, "shell")],
      ["PTY", fieldText(inputRecord, "tty")],
      ["Login shell", fieldText(inputRecord, "login")],
    ]),
    "",
    "**执行结果**",
    toolFieldLines([
      ["命令状态", status ? commandStatusLabel(status) : ""],
      ["Session ID", runningSessionId ? markdownCode(runningSessionId) : ""],
      ["终端 UUID", terminalId ? markdownCode(terminalId) : ""],
      ["退出码", exitCode],
      ["Chunk ID", fieldText(resultRecord, "chunk_id")],
      ["原始 token 数", fieldText(resultRecord, "original_token_count")],
      ["耗时（秒）", fieldText(resultRecord, "wall_time_seconds")],
    ]),
    terminalId
      ? "说明：命令完成不代表终端关闭；可从后台连接面板重新打开终端。"
      : "",
    output ? `\n输出：\n\`\`\`text\n${output}\n\`\`\`` : "",
  ].filter((part) => part.trim().length > 0);

  return sections.join("\n\n") || null;
}

function terminalToolCollapsedText(item: AggregatedToolItem): string | null {
  if (item.toolName !== "exec_command" && item.toolName !== "write_stdin") {
    return null;
  }
  const resultRecord = parseJsonRecord(item.rawEnd.result) ?? {};
  if (fieldText(resultRecord, "session_id")) {
    return item.toolName === "write_stdin"
      ? "已写入，命令仍在运行"
      : "命令仍在运行，终端可 attach";
  }
  if (fieldText(resultRecord, "exit_code")) {
    return "命令已完成，终端仍可打开";
  }
  return "命令工具已返回，终端仍可打开";
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
  if (skillNames(item).length > 0) {
    return true;
  }
  if (item.toolName !== "read_file") {
    return false;
  }
  const inputRecord = parseJsonRecord(item.rawStart.args) ?? {};
  return Boolean(
    skillNameFromPath(
      fieldText(inputRecord, "file_path") || fieldText(inputRecord, "path"),
    ),
  );
}

export function isRoutineInternalToolItem(item: AggregatedToolItem): boolean {
  return ROUTINE_INTERNAL_TOOL_NAMES.has(item.toolName);
}

function formatCustomToolSkillContent(item: AggregatedToolItem): string | null {
  const names = skillNames(item);
  if (names.length === 0) {
    return null;
  }
  const result = item.resultText.trim();
  const input = item.inputText.trim();
  const invocation = invocationToolName(item);
  return [
    `**Skill**\n由 ${names.map(markdownCode).join("、")} 记录的扩展工具调用。`,
    invocation
      ? `**调用入口**\n${markdownCode(invocation)} -> ${markdownCode(item.toolName)}`
      : "",
    input ? `**输入参数**\n\`\`\`\n${input}\n\`\`\`` : "",
    result ? `**执行结果**\n\`\`\`\n${result}\n\`\`\`` : "",
  ].filter((part) => part.trim().length > 0).join("\n\n");
}

function formatCustomInvocationToolContent(item: AggregatedToolItem): string | null {
  const invocation = invocationToolName(item);
  if (!invocation) {
    return null;
  }
  const result = item.resultText.trim();
  const input = item.inputText.trim();
  return [
    `**调用入口**\n${markdownCode(invocation)} -> ${markdownCode(item.toolName)}`,
    input ? `**输入参数**\n\`\`\`\n${input}\n\`\`\`` : "",
    result ? `**执行结果**\n\`\`\`\n${result}\n\`\`\`` : "",
  ].filter((part) => part.trim().length > 0).join("\n\n");
}

export function formatToolCardContent(item: AggregatedToolItem): string | null {
  return formatTerminalToolContent(item) ??
    formatCustomToolSkillContent(item) ??
    formatCustomInvocationToolContent(item);
}

export function toolCollapsedText(item: AggregatedToolItem): string {
  const terminalText = terminalToolCollapsedText(item);
  if (terminalText) {
    return terminalText;
  }
  if (item.outcomeUnknown) {
    return "未确认返回结果";
  }

  const inputRecord = parseJsonRecord(item.rawStart.args) ?? {};
  const resultRecord = parseJsonRecord(item.rawEnd.result);
  const path = fieldText(inputRecord, "file_path") || fieldText(inputRecord, "path");
  const skillName = skillNameFromPath(path);
  if (item.toolName === "read_file" && skillName) {
    return `已读取 skill：${skillName}`;
  }

  const names = skillNames(item);
  const prefix = names.length > 0
    ? `${names.join("、")} 记录的 ${invocationLabel(item)}`
    : invocationLabel(item);
  const result =
    fieldText(resultRecord ?? {}, "stdout") ||
    fieldText(resultRecord ?? {}, "result") ||
    fieldText(resultRecord ?? {}, "content") ||
    item.resultText;
  if (result.trim()) {
    return `${prefix} 返回：${compactInline(result, 80)}`;
  }

  if (path.trim()) {
    return `${prefix} 处理：${compactInline(path, 80)}`;
  }
  return `${prefix} 执行完成`;
}
