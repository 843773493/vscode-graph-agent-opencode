// 请求日志的消息与文本投影原语：把落盘的 request/response 消息压成展示用文本。

import type { LLMRequestLogRecord } from "../../types/backend";
import { isRecord } from "../../utils/jsonDisplay";
import { compactKeyFlowText } from "../skillKeyFlow";

function recordString(value: unknown, key: string): string {
  if (!isRecord(value)) {
    return "";
  }
  const item = value[key];
  return typeof item === "string" ? item : "";
}

export function modelLabel(log: LLMRequestLogRecord): string {
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

export function stringifyContent(
  value: unknown,
  options: { includeReasoning: boolean },
): string {
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

export function responsePreview(log: LLMRequestLogRecord): string {
  return messagePreview(log.response.result, { includeReasoning: false }).trim();
}

export function responsePhaseLabel(log: LLMRequestLogRecord): string {
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

export function compactPreview(text: string, maxLength = 240): string {
  return compactKeyFlowText(text, maxLength);
}
