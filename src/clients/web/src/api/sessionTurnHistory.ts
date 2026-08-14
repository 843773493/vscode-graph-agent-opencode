import type {
  APIResponse,
  SessionTurnBootstrap,
  StaleTurnCursorError,
  TurnDetailBatch,
  TurnDetailBatchRequest,
  TurnPage,
} from "../types/backend";
import {
  HttpRequestError,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./http";

const SESSION_TURN_HISTORY_TIMEOUT_MS = 10_000;
const TURN_DETAIL_WORKER_PARSE_THRESHOLD_BYTES = 256 * 1024;

export class StaleTurnCursorHttpError extends Error {
  constructor(readonly detail: StaleTurnCursorError) {
    super(detail.message);
    this.name = "StaleTurnCursorHttpError";
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

function mapTurnCursorError(error: unknown): never {
  if (
    error instanceof HttpRequestError
    && error.status === 409
    && isStaleTurnCursorError(error.detail)
  ) {
    throw new StaleTurnCursorHttpError(error.detail);
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

export async function listSessionTurns(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  options: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<TurnPage> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 20) });
  if (options.cursor) params.set("cursor", options.cursor);
  const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/turns?${params.toString()}`;
  try {
    return unwrapApiData(await requestJson<APIResponse<TurnPage>>(port, path, {
      headers: workspaceHeader(workspaceId),
      timeoutMs: SESSION_TURN_HISTORY_TIMEOUT_MS,
      signal: options.signal,
    }));
  } catch (error) {
    mapTurnCursorError(error);
  }
}

export async function getSessionTurnDetails(
  port: number,
  sessionId: string,
  turnIds: TurnDetailBatchRequest["turn_ids"],
  workspaceId?: string | null,
  signal?: AbortSignal,
): Promise<TurnDetailBatch> {
  const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/turns/details`;
  try {
    return unwrapApiData(await requestJson<APIResponse<TurnDetailBatch>>(port, path, {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ turn_ids: turnIds }),
      timeoutMs: SESSION_TURN_HISTORY_TIMEOUT_MS,
      parseInWorkerAboveBytes: TURN_DETAIL_WORKER_PARSE_THRESHOLD_BYTES,
      signal,
    }));
  } catch (error) {
    mapTurnCursorError(error);
  }
}
