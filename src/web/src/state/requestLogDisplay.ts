import type { LLMRequestLogRecord } from "../types/backend";
import { isRecord } from "../utils/jsonDisplay";
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
  customToolDisplayCallName,
  customToolTargetNameFromArgs,
} from "./customTools/protocol";

export interface RequestLogDisplayModel {
  model: string;
  requestText: string;
  responseText: string;
  phaseLabel: string;
  toolNames: string[];
  calledToolNames: string[];
}

export interface RequestLogKeyFlow {
  readSkills: string[];
  customInvokerNames: string[];
  customToolNames: string[];
  customToolResults: Array<{ toolName: string; invocationToolName: string; resultText: string }>;
  finalText: string;
}

function recordString(value: unknown, key: string): string {
  if (!isRecord(value)) {
    return "";
  }
  const item = value[key];
  return typeof item === "string" ? item : "";
}

function modelLabel(log: LLMRequestLogRecord): string {
  const requestModel = recordString(log.request, "model_name");
  if (requestModel) {
    return requestModel;
  }

  const responseItems = Array.isArray(log.response.result) ? log.response.result : [];
  for (const item of responseItems) {
    const metadata = isRecord(item) ? item.response_metadata : null;
    const modelName = recordString(metadata, "model_name");
    if (modelName) {
      return modelName;
    }
  }
  return "unknown_model";
}

function stringifyContent(value: unknown, options: { includeReasoning: boolean }): string {
  if (typeof value === "string") {
    return compactKeyFlowText(value, Number.MAX_SAFE_INTEGER);
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === "string") {
          return item;
        }
        if (!isRecord(item)) {
          return "";
        }
        const type = item.type;
        if (type === "reasoning" && !options.includeReasoning) {
          return "";
        }
        if (typeof item.text === "string") {
          return compactKeyFlowText(item.text, Number.MAX_SAFE_INTEGER);
        }
        if (typeof item.reasoning === "string") {
          return item.reasoning;
        }
        if (typeof item.content === "string") {
          return compactKeyFlowText(item.content, Number.MAX_SAFE_INTEGER);
        }
        return "";
      })
      .join("");
  }
  return "";
}

function messagePreview(messages: unknown, options: { includeReasoning: boolean }): string {
  if (!Array.isArray(messages)) {
    return "";
  }
  return messages
    .map((message) => {
      if (!isRecord(message)) {
        return "";
      }
      const content = stringifyContent(message.content, options).trim();
      if (
        content.startsWith("<system_reminder>") &&
        content.endsWith("</system_reminder>")
      ) {
        return "";
      }
      return stringifyContent(message.content, options);
    })
    .filter(Boolean)
    .join("\n");
}

function requestPreview(log: LLMRequestLogRecord): string {
  return messagePreview(log.request.messages, { includeReasoning: true }).trim();
}

function responsePreview(log: LLMRequestLogRecord): string {
  return messagePreview(log.response.result, { includeReasoning: false }).trim();
}

function responsePhaseLabel(log: LLMRequestLogRecord): string {
  const responseItems = Array.isArray(log.response.result) ? log.response.result : [];
  const hasToolCall = responseItems.some((item) => {
    if (!isRecord(item)) {
      return false;
    }
    const calls = item.tool_calls;
    return Array.isArray(calls) && calls.length > 0;
  });
  if (hasToolCall) {
    return "工具调用请求";
  }
  const responseText = responsePreview(log);
  return responseText ? "自然语言回复" : "中间请求";
}

function requestToolNames(log: LLMRequestLogRecord): string[] {
  const tools = Array.isArray(log.request.tools) ? log.request.tools : [];
  const names: string[] = [];
  const seen = new Set<string>();

  for (const tool of tools) {
    let name = "";
    if (isRecord(tool)) {
      if (typeof tool.name === "string") {
        name = tool.name;
      } else if (isRecord(tool.function) && typeof tool.function.name === "string") {
        name = tool.function.name;
      }
    }
    if (!name || seen.has(name)) {
      continue;
    }
    seen.add(name);
    names.push(name);
  }

  return names;
}

function toolCallsFromMessageLike(value: unknown): Record<string, unknown>[] {
  if (!isRecord(value)) {
    return [];
  }
  const directToolCalls = Array.isArray(value.tool_calls) ? value.tool_calls : [];
  const additionalToolCalls =
    isRecord(value.additional_kwargs) && Array.isArray(value.additional_kwargs.tool_calls)
      ? value.additional_kwargs.tool_calls
      : [];
  return [...directToolCalls, ...additionalToolCalls].filter(isRecord);
}

function collectToolCallsFromLog(log: LLMRequestLogRecord): Record<string, unknown>[] {
  const requestMessages = Array.isArray(log.request.messages) ? log.request.messages : [];
  const responseItems = Array.isArray(log.response.result) ? log.response.result : [];
  return [...requestMessages, ...responseItems].flatMap(toolCallsFromMessageLike);
}

function responseCalledToolNames(log: LLMRequestLogRecord): string[] {
  const responseItems = Array.isArray(log.response.result) ? log.response.result : [];
  const names: string[] = [];
  const seen = new Set<string>();

  for (const item of responseItems) {
    if (!isRecord(item)) {
      continue;
    }
    const directToolCalls = Array.isArray(item.tool_calls) ? item.tool_calls : [];
    const additionalToolCalls =
      isRecord(item.additional_kwargs) && Array.isArray(item.additional_kwargs.tool_calls)
        ? item.additional_kwargs.tool_calls
        : [];

    for (const call of [...directToolCalls, ...additionalToolCalls]) {
      const name = customToolDisplayCallName(call);
      if (!name || seen.has(name)) {
        continue;
      }
      seen.add(name);
      names.push(name);
    }
  }

  return names;
}

function compactPreview(text: string, maxLength = 240): string {
  return compactKeyFlowText(text, maxLength);
}

function uniquePush(items: string[], seen: Set<string>, value: string): void {
  if (!value || seen.has(value)) {
    return;
  }
  seen.add(value);
  items.push(value);
}

function collectToolResultMessages(
  log: LLMRequestLogRecord,
): Array<{ toolName: string; toolCallId: string; resultText: string }> {
  const messages = Array.isArray(log.request.messages) ? log.request.messages : [];
  const results: Array<{ toolName: string; toolCallId: string; resultText: string }> = [];
  for (const message of messages) {
    if (!isRecord(message)) {
      continue;
    }
    const type = typeof message.type === "string" ? message.type : "";
    const role = typeof message.role === "string" ? message.role : "";
    const toolName = typeof message.name === "string" ? message.name : "";
    if (!toolName || (type !== "tool" && role !== "tool")) {
      continue;
    }
    const resultText = compactPreview(
      stringifyContent(message.content, { includeReasoning: false }),
      120,
    );
    if (resultText) {
      results.push({
        toolName,
        toolCallId: customToolCallId(message),
        resultText,
      });
    }
  }
  return results;
}

function isCustomInvokerValidationError(resultText: string): boolean {
  return (
    resultText.includes("Error invoking tool 'invoke_custom_tool'") &&
    resultText.includes("tool_name: Field required")
  );
}

export function buildRequestLogDisplay(log: LLMRequestLogRecord): RequestLogDisplayModel {
  const responseText = compactPreview(responsePreview(log));
  return {
    model: modelLabel(log),
    requestText: compactPreview(requestPreview(log)),
    responseText,
    phaseLabel: responsePhaseLabel(log),
    toolNames: requestToolNames(log),
    calledToolNames: responseCalledToolNames(log),
  };
}

export function buildRequestLogKeyFlow(logs: LLMRequestLogRecord[]): RequestLogKeyFlow {
  const orderedLogs = [...logs].sort((left, right) => left.timestamp - right.timestamp);
  const state = createSkillKeyFlowState();
  const customInvokerNames: string[] = [];
  const customToolNames: string[] = [];
  const customToolResults: Array<{ toolName: string; invocationToolName: string; resultText: string }> = [];
  const seenCustomInvokers = new Set<string>();
  const seenCustomTools = new Set<string>();
  const seenResults = new Set<string>();
  const customToolTargetsByCallId = new Map<string, string>();

  for (const log of orderedLogs) {
    for (const toolName of requestToolNames(log)) {
      if (toolName === CUSTOM_TOOL_INVOKER_NAME) {
        uniquePush(customInvokerNames, seenCustomInvokers, toolName);
      }
    }

    for (const call of collectToolCallsFromLog(log)) {
      const name = customToolCallName(call);
      if (name === CUSTOM_TOOL_INVOKER_NAME) {
        const customToolName = customToolTargetNameFromArgs(customToolCallArgs(call));
        uniquePush(customToolNames, seenCustomTools, customToolName);
        const callId = customToolCallId(call);
        if (customToolName && callId) {
          customToolTargetsByCallId.set(callId, customToolName);
        }
      }

      if (name === "read_file") {
        recordReadSkill(state, { toolName: name, args: customToolCallArgs(call) });
      }
    }

    const responseText = responsePreview(log);
    if (responseText.trim()) {
      recordFinalText(state, responseText);
    }
  }

  for (const log of orderedLogs) {
    for (const result of collectToolResultMessages(log)) {
      if (result.toolName !== CUSTOM_TOOL_INVOKER_NAME) {
        continue;
      }
      const customToolName = result.toolCallId
        ? customToolTargetsByCallId.get(result.toolCallId)
        : "";
      const displayToolName = customToolName ||
        (isCustomInvokerValidationError(result.resultText)
          ? INVALID_CUSTOM_TOOL_CALL_NAME
          : UNKNOWN_CUSTOM_TOOL_NAME);
      const key = `${result.toolCallId || "missing-id"}\u0000${displayToolName}\u0000${CUSTOM_TOOL_INVOKER_NAME}\u0000${result.resultText}`;
      if (seenResults.has(key)) {
        continue;
      }
      seenResults.add(key);
      customToolResults.push({
        toolName: displayToolName,
        invocationToolName: CUSTOM_TOOL_INVOKER_NAME,
        resultText: result.resultText,
      });
    }
  }
  const snapshot = skillKeyFlowSnapshot(state);

  return {
    readSkills: snapshot.readSkills,
    customInvokerNames,
    customToolNames,
    customToolResults,
    finalText: snapshot.finalText,
  };
}
