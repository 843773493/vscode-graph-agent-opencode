// 请求日志关键流转投影：跨多条日志聚合读取 skill、扩展工具入口与最终文本。

import type { LLMRequestLogRecord } from "../../types/backend";
import { isRecord } from "../../utils/jsonDisplay";
import {
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
  customToolTargetNameFromArgs,
  isCustomInvokerValidationError,
} from "../customTools/protocol";
import { compactPreview, responsePreview, stringifyContent } from "./messages";
import { collectToolCallsFromLog, requestToolNames } from "./tools";
import type { RequestLogKeyFlow } from "./types";

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
      if (toolName === EXTENSION_TOOL_INVOKER_NAME) {
        uniquePush(customInvokerNames, seenCustomInvokers, toolName);
      }
    }

    for (const call of collectToolCallsFromLog(log)) {
      const name = customToolCallName(call);
      if (name === EXTENSION_TOOL_INVOKER_NAME) {
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
      if (result.toolName !== EXTENSION_TOOL_INVOKER_NAME) {
        continue;
      }
      const customToolName = result.toolCallId
        ? customToolTargetsByCallId.get(result.toolCallId)
        : "";
      const displayToolName = customToolName ||
        (isCustomInvokerValidationError(result.resultText)
          ? INVALID_CUSTOM_TOOL_CALL_NAME
          : UNKNOWN_CUSTOM_TOOL_NAME);
      const key = `${result.toolCallId || "missing-id"}\u0000${displayToolName}\u0000${EXTENSION_TOOL_INVOKER_NAME}\u0000${result.resultText}`;
      if (seenResults.has(key)) {
        continue;
      }
      seenResults.add(key);
      customToolResults.push({
        toolName: displayToolName,
        invocationToolName: EXTENSION_TOOL_INVOKER_NAME,
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
