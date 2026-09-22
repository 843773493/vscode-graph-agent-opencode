import { normalizeDisplayText } from "../../utils/displayText";
import { isRecord, redactLargeData } from "../../utils/jsonDisplay";
import { allowedToolsFromSkillMarkdownText } from "../../utils/markdown/skillMarkdown";
import {
  compactKeyFlowText,
  createSkillKeyFlowState,
  recordFinalText,
  recordReadSkill,
  skillKeyFlowSnapshot,
} from "../skillKeyFlow";
import {
  EXTENSION_TOOL_INVOKER_NAME,
  INVALID_CUSTOM_TOOL_CALL_NAME,
  UNKNOWN_CUSTOM_TOOL_NAME,
  customToolCallArgs,
  customToolCallId,
  customToolCallName,
  customToolTargetNameFromCall,
  isCustomInvokerValidationError,
} from "../customTools/protocol";
import { errorMessage } from "../../utils/errorMessage";

export interface AgentStateSummary {
  skills: string[];
  customTools: string[];
  customToolResults: Array<{ toolName: string; invocationToolName: string; resultText: string }>;
  finalText: string;
}

/** 非法行保留的原文片段上限：损坏行本身可能极长，不能让它淹没面板。 */
const AGENT_STATE_INVALID_LINE_SNIPPET_LIMIT = 400;

/** 单行 Agent State 的解析结果：成功值，或带原文片段的解析失败。 */
export type AgentStateJsonlLine =
  | { ok: true; lineNumber: number; value: unknown }
  | { ok: false; lineNumber: number; raw: string; message: string };

/**
 * Agent State JSONL 的唯一解析原语。
 *
 * 面板在 **render 阶段** 直接消费它，因此任何非法、截断或超长的行都不得把异常
 * 抛出组件树（否则会被 AppErrorBoundary 顶上、整块白屏）；这与同族
 * `state/display/toolDisplay.parseJsonRecord` 用 try/catch 挡住同一种失败的口径一致。
 * 但与 toolDisplay 不同，这里 **不静默丢弃** 非法行：原始 JSONL 快照是排查
 * checkpoint 与消息格式的唯一依据，失败行必须连同原文片段一起呈现给用户。
 */
export function parseAgentStateJsonlLines(jsonl: string): AgentStateJsonlLine[] {
  const lines: AgentStateJsonlLine[] = [];
  jsonl.split(/\r?\n/).forEach((raw, index) => {
    if (raw.trim().length === 0) return;
    const lineNumber = index + 1;
    try {
      lines.push({ ok: true, lineNumber, value: JSON.parse(raw) });
    } catch (error) {
      lines.push({
        ok: false,
        lineNumber,
        raw: raw.length > AGENT_STATE_INVALID_LINE_SNIPPET_LIMIT
          ? `${raw.slice(0, AGENT_STATE_INVALID_LINE_SNIPPET_LIMIT)}...`
          : raw,
        message: errorMessage(error),
      });
    }
  });
  return lines;
}

/** 解析失败行的可见标注：保留原文片段，便于直接定位损坏的那一行。 */
export function agentStateInvalidLineNote(
  line: Extract<AgentStateJsonlLine, { ok: false }>,
): string {
  return `[[第 ${line.lineNumber} 行解析失败：${line.message}；原文片段：${line.raw}]]`;
}

/** 供组件渲染的逐行文本：成功行脱敏后重新序列化，失败行标注原文片段。 */
export function formatAgentStateLinesForDisplay(lines: AgentStateJsonlLine[]): string {
  return lines
    .map((line) => line.ok
      ? JSON.stringify(redactLargeData(line.value))
      : agentStateInvalidLineNote(line))
    .join("\n");
}

/** 从已解析行中取出可用的记录：非法行被排除，但调用方仍可据 lines 给出诊断。 */
export function agentStateRecordsFromLines(
  lines: AgentStateJsonlLine[],
): Record<string, unknown>[] {
  return lines
    .filter((line): line is Extract<AgentStateJsonlLine, { ok: true }> => line.ok)
    .map((line) => line.value)
    .filter(isRecord);
}

export function parseAgentStateRecords(jsonl: string): Record<string, unknown>[] {
  return agentStateRecordsFromLines(parseAgentStateJsonlLines(jsonl));
}

function agentStateMessageId(record: Record<string, unknown>): string | null {
  const responseMetadata = isRecord(record.response_metadata)
    ? record.response_metadata
    : null;
  if (!responseMetadata) return null;
  if (typeof responseMetadata.message_id === "string") {
    return responseMetadata.message_id;
  }
  const nestedMetadata = isRecord(responseMetadata.message_metadata)
    ? responseMetadata.message_metadata
    : null;
  return nestedMetadata && typeof nestedMetadata.message_id === "string"
    ? nestedMetadata.message_id
    : null;
}

function formatAgentStateMessageContent(content: unknown): string {
  if (typeof content === "string") return content;
  return JSON.stringify(content, null, 2) ?? String(content);
}

/** 根据稳定 message_id 找到原始 Agent State 消息正文，供显式调试展开使用。 */
export function findAgentStateMessageRawContent(
  jsonl: string,
  messageId: string,
): string | null {
  if (!messageId) return null;
  const record = parseAgentStateRecords(jsonl).find(
    (candidate) => agentStateMessageId(candidate) === messageId,
  );
  return record ? formatAgentStateMessageContent(record.content) : null;
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
      if (toolName === EXTENSION_TOOL_INVOKER_NAME) {
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
            invocationToolName: EXTENSION_TOOL_INVOKER_NAME,
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
