import type {
  APIResponse,
  CursorPage,
  SessionActivity,
} from "../../types/backend";
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from "../../sse/sseClient";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import {
  HttpRequestError,
  requestGatewayResponse,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "../http";

export class SessionActivityCursorGoneError extends Error {
  readonly status = 410;

  constructor(readonly cursor: number) {
    super(`Workspace 会话活动游标已失效: ${cursor}`);
    this.name = "SessionActivityCursorGoneError";
  }
}

export async function listSessionActivity(
  port: number,
  workspaceId: string,
  options: { after?: number; limit?: number } = {},
): Promise<CursorPage<SessionActivity>> {
  const params = new URLSearchParams({
    after: String(options.after ?? 0),
    limit: String(options.limit ?? 200),
  });
  try {
    return unwrapApiData(
      await requestJson<APIResponse<CursorPage<SessionActivity>>>(
        port,
        `/api/v1/session-catalog/events?${params.toString()}`,
        { headers: workspaceHeader(workspaceId) },
      ),
    );
  } catch (error: unknown) {
    if (error instanceof HttpRequestError && error.status === 410) {
      throw new SessionActivityCursorGoneError(options.after ?? 0);
    }
    throw error;
  }
}

export async function streamSessionActivity(
  port: number,
  workspaceId: string,
  options: {
    after?: number;
    signal?: AbortSignal;
    onEvent?: (event: SessionActivity, cursor: number) => void;
    onActivity?: () => void;
  } = {},
): Promise<void> {
  // SSE 长期连接只共享统一凭据与刷新重试；生命周期内的断线重连由调用方负责。
  let response: Response;
  try {
    response = await requestGatewayResponse(
      port,
      "/api/v1/session-catalog/events/stream",
      {
        signal: options.signal,
        skipGatewayUserSession: true,
        headers: {
          accept: "text/event-stream",
          ...workspaceHeader(workspaceId),
          ...(options.after !== undefined
            ? { "Last-Event-ID": String(options.after) }
            : {}),
        },
      },
    );
  } catch (error) {
    if (error instanceof HttpRequestError && error.status === 410) {
      throw new SessionActivityCursorGoneError(options.after ?? 0);
    }
    throw error;
  }
  if (!response.body) {
    throw new Error(`无法连接 Workspace 会话活动流: ${response.status} ${response.statusText}`);
  }
  await consumeSseResponse(response, {
    signal: options.signal,
    idleTimeoutMs: SSE_IDLE_TIMEOUT_MS,
    onActivity: options.onActivity,
    events: {
      session_activity: defineSseEvent(
        (data, frame) => {
          if (!frame.id) throw new Error("Workspace 会话活动流缺少 id 行");
          const cursor = Number(frame.id);
          if (!Number.isSafeInteger(cursor) || cursor < 1) {
            throw new Error(`Workspace 会话活动游标无效: ${frame.id}`);
          }
          return {
            cursor,
            event: decodeJsonSseData(data, frame) as SessionActivity,
          };
        },
        ({ cursor, event }) => options.onEvent?.(event, cursor),
      ),
      cursor_gone: defineSseEvent(
        (data, frame) => decodeJsonSseData(data, frame),
        () => {
          throw new SessionActivityCursorGoneError(options.after ?? 0);
        },
      ),
    },
  });
}
