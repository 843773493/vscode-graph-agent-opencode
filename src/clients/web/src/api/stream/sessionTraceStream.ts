import type {
  APIResponse,
  CursorPage,
  SessionStreamEvent,
  TraceEvent,
} from "../../types/backend";
import { decodeJsonSseData, defineSseEvent } from "../../sse/sseClient";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import { validateTraceEvent } from "../../sse/sseRuntimeSchemas";
import {
  HttpRequestError,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "../http";
import { consumeWorkspaceObservation } from "./workspaceObservation";

const SESSION_TRACE_TIMEOUT_MS = 10_000;

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
    threadId?: string;
  } = {},
): Promise<CursorPage<TraceEvent>> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 100) });
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.threadId) params.set("thread_id", options.threadId);
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
    const page = unwrapApiData(result);
    for (const item of page.items) {
      validateTraceOwner(item, sessionId, options.threadId);
    }
    return page;
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
    threadId?: string;
    afterCursor?: string | null;
    onEvent?: (event: SessionStreamEvent, cursor: string) => void;
    onError?: (error: unknown) => void;
    onActivity?: () => void;
    onConnected?: (routeRevision: string | null) => void;
    idleTimeoutMs?: number;
    signal?: AbortSignal;
  },
): Promise<void> {
  let afterCursor = options?.afterCursor ?? undefined;
  let connected = false;
  const idleTimeoutMs = options?.idleTimeoutMs ?? SSE_IDLE_TIMEOUT_MS;
  const deliveredCursors = new Set<string>();
  try {
    await consumeWorkspaceObservation(port, workspaceHeader(options?.workspaceId), () => ({
      kind: "session_trace", session_id: sessionId, trace_after: afterCursor, paths: [],
      thread_id: options?.threadId,
    }), {
      signal: options?.signal,
      idleTimeoutMs,
      idleTimeoutError: (timeoutMs) => new SessionStreamIdleTimeoutError(timeoutMs),
      onActivity: options?.onActivity,
      onConnected: (routeRevision) => {
        connected = true;
        options?.onConnected?.(routeRevision);
      },
      events: {
        trace: defineSseEvent(
          (data, frame) => {
            if (!frame.id) throw new Error("SSE trace 缺少 id 行");
            if (deliveredCursors.has(frame.id)) {
              throw new Error(`SSE trace 重复 cursor: ${frame.id}`);
            }
            deliveredCursors.add(frame.id);
            const event = validateTraceOwner(decodeJsonSseData(data, frame), sessionId, options?.threadId);
            return {
              cursor: frame.id,
              event,
            };
          },
          ({ cursor, event }) => {
            options?.onEvent?.(event, cursor);
            afterCursor = cursor;
          },
        ),
      },
    });
  } catch (error) {
    if (options?.signal?.aborted) return;
    if (error instanceof HttpRequestError && error.status === 410) {
      throw new TraceCursorGoneError(afterCursor ?? "");
    }
    if (connected) options?.onError?.(error);
    throw error;
  }
}

function validateTraceOwner(value: unknown, sessionId: string, threadId?: string): SessionStreamEvent {
  const event = validateTraceEvent(value);
  if (event.session_id !== sessionId || (threadId !== undefined && event.thread_id !== threadId)) {
    throw new Error(`Trace owner 不匹配: 请求 ${sessionId}/${threadId ?? "*"}，收到 ${event.session_id}/${event.thread_id}`);
  }
  return event;
}
