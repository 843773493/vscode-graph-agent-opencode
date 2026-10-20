// 请求日志关键流转投影：聚合 Skill 加载回执、扩展工具入口与最终文本。

import type { LLMRequestLogRecord } from "../../types/backend";
import { isRecord } from "../../utils/jsonDisplay";
import {
  createSkillKeyFlowState,
  recordFinalText,
  recordSkillLoadResult,
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

function* collectToolResultMessages(
  log: LLMRequestLogRecord,
): Iterable<{ toolName: string; toolCallId: string; resultText: string }> {
  const messages = Array.isArray(log.request.messages) ? log.request.messages : [];
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
    const resultText = stringifyContent(message.content, { includeReasoning: false });
    if (resultText) {
      yield {
        toolName,
        toolCallId: customToolCallId(message),
        resultText,
      };
    }
  }
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
    }

    const responseText = responsePreview(log);
    if (responseText.trim()) {
      recordFinalText(state, responseText);
    }
  }

  for (const log of orderedLogs) {
    for (const result of collectToolResultMessages(log)) {
      recordSkillLoadResult(state, { toolName: result.toolName, result: result.resultText });
      if (result.toolName !== EXTENSION_TOOL_INVOKER_NAME) {
        continue;
      }
      const resultText = compactPreview(result.resultText, 120);
      const customToolName = result.toolCallId
        ? customToolTargetsByCallId.get(result.toolCallId)
        : "";
      const displayToolName = customToolName ||
        (isCustomInvokerValidationError(resultText)
          ? INVALID_CUSTOM_TOOL_CALL_NAME
          : UNKNOWN_CUSTOM_TOOL_NAME);
      const key = `${result.toolCallId || "missing-id"}\u0000${displayToolName}\u0000${EXTENSION_TOOL_INVOKER_NAME}\u0000${resultText}`;
      if (seenResults.has(key)) {
        continue;
      }
      seenResults.add(key);
      customToolResults.push({
        toolName: displayToolName,
        invocationToolName: EXTENSION_TOOL_INVOKER_NAME,
        resultText,
      });
    }
  }
  const snapshot = skillKeyFlowSnapshot(state);

  return {
    acceptedSkillLoads: snapshot.acceptedSkillLoads,
    customInvokerNames,
    customToolNames,
    customToolResults,
    finalText: snapshot.finalText,
  };
}
