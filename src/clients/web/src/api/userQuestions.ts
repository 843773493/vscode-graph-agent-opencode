import type { APIResponse } from "../types/backend";
import type { UserQuestionPageDTO, UserQuestionSummaryDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import { validateUserQuestionSummaryDTO } from "../../../../shared/sseRuntimeValidators.js";
import { consumeSseResponse, decodeJsonSseData, defineSseEvent, SseProtocolError } from "../sse/sseClient";
import { validateSseError } from "../sse/sseRuntimeSchemas";
import { HttpRequestError, requestGatewayResponse, requestJson, unwrapApiData, workspaceHeader } from "./http";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS, sessionStreamReconnectDelay } from "../hooks/sessionEventStream/sessionEventStreamPolicy";
import { waitForReconnect } from "../hooks/sessionEventStream/waitForReconnect";

export type { UserQuestionPageDTO, UserQuestionSummaryDTO };
export interface UserQuestionOwner { port: number; workspaceKey: string; sessionId: string; threadId: string }
const path = (owner: UserQuestionOwner) => `/api/v1/sessions/${encodeURIComponent(owner.sessionId)}/threads/${encodeURIComponent(owner.threadId)}/questions`;

export async function listUserQuestions(owner: UserQuestionOwner, signal: AbortSignal, before?: number) {
  return unwrapApiData(await requestJson<APIResponse<UserQuestionPageDTO>>(owner.port,
    path(owner) + (before === undefined ? "" : `?before=${before}`), { signal, headers: workspaceHeader(owner.workspaceKey), cache: "no-store" }));
}
export async function deleteUserQuestion(owner: UserQuestionOwner, questionId: string, signal: AbortSignal) {
  return unwrapApiData(await requestJson<APIResponse<UserQuestionSummaryDTO>>(owner.port,
    `${path(owner)}/${encodeURIComponent(questionId)}`, { method: "DELETE", signal, headers: workspaceHeader(owner.workspaceKey) }));
}

class QuestionStreamDisconnected extends Error {}

/** 只订阅数量与版本；每次重连获得当前快照，不补读留言正文或工具历史。 */
export async function watchUserQuestions(owner: UserQuestionOwner, signal: AbortSignal, onSummary: (value: UserQuestionSummaryDTO) => void) {
  let attempt = 0;
  while (!signal.aborted) {
    try {
      const response = await requestGatewayResponse(owner.port, path(owner) + "/events", {
        signal, headers: { ...workspaceHeader(owner.workspaceKey), accept: "text/event-stream" }, cache: "no-store",
      });
      await consumeSseResponse(response, { signal, readError: cause => new QuestionStreamDisconnected("留言订阅读取失败", { cause }), events: {
        changed: defineSseEvent((data, frame) => {
          const value: unknown = decodeJsonSseData(data, frame);
          if (!validateUserQuestionSummaryDTO(value)) throw new SseProtocolError("留言摘要协议无效");
          const summary = value as UserQuestionSummaryDTO;
          if (summary.session_id !== owner.sessionId || summary.thread_id !== owner.threadId) throw new SseProtocolError("留言归属不一致");
          return summary;
        }, summary => { attempt = 0; onSummary(summary); }),
        error: defineSseEvent((data, frame) => validateSseError(decodeJsonSseData(data, frame)), error => { throw new SseProtocolError(error.message); }),
      } });
      if (!signal.aborted) throw new QuestionStreamDisconnected("留言订阅已关闭");
    } catch (error) {
      if (signal.aborted) return;
      const retryable = error instanceof QuestionStreamDisconnected || error instanceof TypeError
        || error instanceof HttpRequestError && error.status >= 500;
      if (!retryable || attempt >= SESSION_STREAM_MAX_RECONNECT_ATTEMPTS) throw error;
      await waitForReconnect(signal, sessionStreamReconnectDelay(attempt++));
    }
  }
}
