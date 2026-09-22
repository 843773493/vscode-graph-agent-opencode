import type {
  APIResponse,
  CursorPage,
  SessionStreamEvent,
  TraceEvent,
} from "../../types/backend";
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from "../../sse/sseClient";
import { validateTraceEvent } from "../../sse/sseRuntimeSchemas";
import {
  HttpRequestError,
  requestGatewayResponse,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "../http";

const SESSION_TRACE_TIMEOUT_MS = 10_000;
const DEFAULT_SESSION_STREAM_IDLE_TIMEOUT_MS = 45_000;

export class TraceCursorGoneError extends Error {
  readonly status = 410;

  constructor(readonly cursor: string) {
    super(`Trace 事件游标已失效: ${cursor}`);
    this.name = "TraceCursorGoneError";
  }
}

export class SessionStreamIdleTimeoutError extends Error {
  constructor(readonly timeoutMs: number) {
    super(`会话事件流超过 ${timeoutMs}ms 未收到任何数据`);
    this.name = "SessionStreamIdleTimeoutError";
  }
}

export async function listSessionTraceHistory(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  options: {
    cursor?: string | null;
    limit?: number;
    signal?: AbortSignal;
  } = {},
): Promise<CursorPage<TraceEvent>> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 100) });
  if (options.cursor) params.set("cursor", options.cursor);
  try {
    const result = await requestJson<APIResponse<CursorPage<TraceEvent>>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces?${params.toString()}`,
      {
        headers: workspaceHeader(workspaceId),
        timeoutMs: SESSION_TRACE_TIMEOUT_MS,
        signal: options.signal,
      },
    );
    return unwrapApiData(result);
  } catch (error) {
    if (error instanceof HttpRequestError && error.status === 410) {
      throw new TraceCursorGoneError(options.cursor ?? "");
    }
    throw error;
  }
}

export async function streamSessionEvents(
  port: number,
  sessionId: string,
  options?: {
    workspaceId?: string | null;
    afterCursor?: string | null;
    onEvent?: (event: SessionStreamEvent, cursor: string) => void;
    onError?: (error: unknown) => void;
    onActivity?: () => void;
    onConnected?: (routeRevision: string | null) => void;
    idleTimeoutMs?: number;
    signal?: AbortSignal;
  },
): Promise<void> {
  // SSE 长期连接只共享统一凭据与刷新重试；生命周期内的断线重连由调用方负责。
  let response: Response;
  try {
    response = await requestGatewayResponse(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces/stream`,
      {
        signal: options?.signal,
        skipGatewayUserSession: true,
        headers: {
          accept: "text/event-stream",
          ...workspaceHeader(options?.workspaceId),
          ...(options?.afterCursor ? { "Last-Event-ID": options.afterCursor } : {}),
        },
      },
    );
  } catch (error) {
    if (error instanceof HttpRequestError && error.status === 410) {
      throw new TraceCursorGoneError(options?.afterCursor ?? "");
    }
    throw error;
  }
  if (!response.body) {
    throw new Error(`无法连接会话事件流: ${response.status} ${response.statusText}`);
  }
  options?.onConnected?.(response.headers.get("X-BoxTeam-Route-Revision"));
  const idleTimeoutMs = options?.idleTimeoutMs ?? DEFAULT_SESSION_STREAM_IDLE_TIMEOUT_MS;
  try {
    await consumeSseResponse(response, {
      signal: options?.signal,
      idleTimeoutMs,
      idleTimeoutError: (timeoutMs) => new SessionStreamIdleTimeoutError(timeoutMs),
      onActivity: options?.onActivity,
      events: {
        trace: defineSseEvent(
          (data, frame) => {
            if (!frame.id) throw new Error("SSE trace 缺少 id 行");
            return {
              cursor: frame.id,
              event: validateTraceEvent(decodeJsonSseData(data, frame)),
            };
          },
          ({ cursor, event }) => options?.onEvent?.(event, cursor),
        ),
      },
    });
  } catch (error) {
    if (options?.signal?.aborted) return;
    options?.onError?.(error);
    throw error;
  }
}
