import type {
  APIResponse,
  SessionTurnBootstrap,
  StaleTurnCursorError,
  StaleTurnReferenceError,
  TurnHistoryLoadRequest,
  TurnHistoryPage,
} from "../types/backend";
import {
  HttpRequestError,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./http";

const SESSION_TURN_HISTORY_TIMEOUT_MS = 10_000;
const TURN_DETAIL_WORKER_PARSE_THRESHOLD_BYTES = 256 * 1024;

export type TurnHistoryInclude =
  | "user"
  | "text"
  | "reasoning_summary"
  | "reasoning_detail"
  | "encrypted_reasoning_meta"
  | "assistant_text"
  | "assistant"
  | "tool_summary"
  | "tool_call"
  | "tool_result"
  | "thinking"
  | "internal"
  | "metadata"
  | "final_response";

export type TurnHistoryLoadRequestPayload = Omit<TurnHistoryLoadRequest, "include"> & {
  include?: TurnHistoryInclude[];
};

export class StaleTurnCursorHttpError extends Error {
  constructor(readonly detail: StaleTurnCursorError) {
    super(detail.message);
    this.name = "StaleTurnCursorHttpError";
  }
}

export class StaleTurnReferenceHttpError extends Error {
  constructor(readonly detail: StaleTurnReferenceError) {
    super(detail.message);
    this.name = "StaleTurnReferenceHttpError";
  }
}

function isStaleTurnCursorError(value: unknown): value is StaleTurnCursorError {
  if (!value || typeof value !== "object") return false;
  const detail = value as Partial<StaleTurnCursorError>;
  return detail.code === "stale_turn_cursor"
    && typeof detail.session_id === "string"
    && typeof detail.cursor_epoch === "number"
    && typeof detail.current_epoch === "number"
    && typeof detail.message === "string";
}

function isStaleTurnReferenceError(value: unknown): value is StaleTurnReferenceError {
  if (!value || typeof value !== "object") return false;
  const detail = value as Partial<StaleTurnReferenceError>;
  return detail.code === "stale_turn_reference"
    && typeof detail.session_id === "string"
    && Array.isArray(detail.turn_ids)
    && detail.turn_ids.every((turnId) => typeof turnId === "string")
    && typeof detail.message === "string";
}

function mapTurnCursorError(error: unknown): never {
  if (
    error instanceof HttpRequestError
    && error.status === 409
    && isStaleTurnCursorError(error.detail)
  ) {
    throw new StaleTurnCursorHttpError(error.detail);
  }
  if (
    error instanceof HttpRequestError
    && error.status === 409
    && isStaleTurnReferenceError(error.detail)
  ) {
    throw new StaleTurnReferenceHttpError(error.detail);
  }
  throw error;
}

export async function getSessionTurnBootstrap(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  signal?: AbortSignal,
): Promise<SessionTurnBootstrap> {
  const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/bootstrap`;
  try {
    return unwrapApiData(await requestJson<APIResponse<SessionTurnBootstrap>>(port, path, {
      headers: workspaceHeader(workspaceId),
      timeoutMs: SESSION_TURN_HISTORY_TIMEOUT_MS,
      signal,
    }));
  } catch (error) {
    mapTurnCursorError(error);
  }
}

export async function loadSessionHistory(
  port: number,
  sessionId: string,
  request: TurnHistoryLoadRequestPayload,
  workspaceId?: string | null,
  signal?: AbortSignal,
): Promise<TurnHistoryPage> {
  const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/history`;
  try {
    return unwrapApiData(await requestJson<APIResponse<TurnHistoryPage>>(port, path, {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify(request),
      timeoutMs: SESSION_TURN_HISTORY_TIMEOUT_MS,
      parseInWorkerAboveBytes: TURN_DETAIL_WORKER_PARSE_THRESHOLD_BYTES,
      signal,
    }));
  } catch (error) {
    mapTurnCursorError(error);
  }
}
