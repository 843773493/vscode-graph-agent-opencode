// 单条请求日志的概览投影：条目摘要与上游尝试列表。

import type { LLMRequestLogRecord } from "../../types/backend";
import { isRecord } from "../../utils/jsonDisplay";
import {
  compactPreview,
  modelLabel,
  responsePhaseLabel,
  responsePreview,
} from "./messages";
import { requestToolNames, responseCalledToolNames } from "./tools";
import type { RequestLogDisplayModel, UpstreamAttemptDisplay } from "./types";

export function buildRequestLogDisplay(log: LLMRequestLogRecord): RequestLogDisplayModel {
  const responseText = compactPreview(responsePreview(log));
  const requestMessages = Array.isArray(log.request.messages) ? log.request.messages : [];
  const responseMessages = Array.isArray(log.response.result) ? log.response.result : [];
  return {
    model: modelLabel(log),
    responseText,
    phaseLabel: responsePhaseLabel(log),
    toolNames: requestToolNames(log),
    calledToolNames: responseCalledToolNames(log),
    messageCount: requestMessages.length,
    responseMessageCount: responseMessages.length,
  };
}

export function buildUpstreamAttemptDisplay(
  log: LLMRequestLogRecord,
): UpstreamAttemptDisplay[] {
  const attempts = isRecord(log.upstream) && Array.isArray(log.upstream.attempts)
    ? log.upstream.attempts
    : [];
  return attempts.filter(isRecord).map((attempt) => ({
    callType: typeof attempt.call_type === "string" ? attempt.call_type : "unknown",
    provider: typeof attempt.provider === "string" ? attempt.provider : "unknown",
    model: typeof attempt.model === "string" ? attempt.model : "unknown",
    apiBase: typeof attempt.api_base === "string" ? attempt.api_base : "",
    request: attempt.request ?? null,
    response: attempt.response ?? null,
    error: attempt.error ?? null,
  }));
}
