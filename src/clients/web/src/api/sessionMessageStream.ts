import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from "../sseClient";
import {
  getApiBaseUrl,
  getGatewayToken,
  HttpRequestError,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./http";
import type {
  MessageStreamEvent,
  MessageStreamEventType,
} from "../state/messageStream";
import type { APIResponse } from "../types/backend";

export class MessageStreamCursorGoneError extends Error {
  readonly status = 410;

  constructor(readonly afterSeq: number) {
    super(`消息流 event_seq 游标已失效: ${afterSeq}`);
    this.name = "MessageStreamCursorGoneError";
  }
}

export interface MessageStreamSnapshotResponse {
  session_id: string;
  turn_id: string;
  turn_stream_id: string;
  snapshot_seq: number;
  stream_status: string;
  agent_loop_status: string;
  current_model_call_id?: string | null;
  current_attempt: number;
  blocks: Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>;
  tool_executions: Array<Record<string, unknown>>;
  model_calls?: Array<Record<string, unknown>>;
  activities?: Array<Record<string, unknown>>;
  resource_refs?: Array<Record<string, unknown>>;
  active_state?: Record<string, unknown> | null;
  recovery?: Record<string, unknown> | null;
  interrupt_state?: Record<string, unknown> | null;
  failure?: Record<string, unknown> | null;
  resumable: boolean;
}

export async function getSessionMessageStreamSnapshot(
  port: number,
  sessionId: string,
  turnId: string,
  options: {
    workspaceId?: string | null;
    turnStreamId?: string | null;
    signal?: AbortSignal;
  } = {},
): Promise<MessageStreamSnapshotResponse> {
  const params = new URLSearchParams();
  if (options.turnStreamId) params.set("turn_stream_id", options.turnStreamId);
  try {
    const response = await requestJson<APIResponse<MessageStreamSnapshotResponse>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/message-stream/snapshot?${params.toString()}`,
      {
        headers: workspaceHeader(options.workspaceId),
        signal: options.signal,
      },
    );
    return unwrapApiData(response);
  } catch (error) {
    if (error instanceof HttpRequestError && error.status === 410) {
      throw new MessageStreamCursorGoneError(0);
    }
    throw error;
  }
}

export async function streamSessionMessageEvents(
  port: number,
  sessionId: string,
  turnId: string,
  options: {
    workspaceId?: string | null;
    turnStreamId?: string | null;
    afterSeq?: number;
    onEvent?: (event: MessageStreamEvent) => void;
    onActivity?: () => void;
    onConnected?: (turnStreamId: string | null) => void;
    signal?: AbortSignal;
  } = {},
): Promise<void> {
  const params = new URLSearchParams();
  if (options.turnStreamId) params.set("turn_stream_id", options.turnStreamId);
  if (options.afterSeq !== undefined) params.set("after_seq", String(options.afterSeq));
  const url = `${getApiBaseUrl(port)}/api/v1/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/message-stream?${params.toString()}`;
  const localToken = await getGatewayToken(port);
  const response = await fetch(url, {
    signal: options.signal,
    headers: {
      accept: "text/event-stream",
      "X-Local-Token": localToken,
      ...workspaceHeader(options.workspaceId),
      ...(options.afterSeq !== undefined
        ? { "Last-Event-ID": String(options.afterSeq) }
        : {}),
    },
  });
  if (response.status === 410) {
    throw new MessageStreamCursorGoneError(options.afterSeq ?? 0);
  }
  if (!response.ok || !response.body) {
    throw new Error(`无法连接 Turn 消息流: ${response.status} ${response.statusText}`);
  }
  options.onConnected?.(response.headers.get("X-Message-Stream-ID"));
  await consumeSseResponse(response, {
    signal: options.signal,
    onActivity: options.onActivity,
    events: {
      "*": defineSseEvent(
        (data, frame) => {
          if (!frame.id) throw new Error("SSE 消息流缺少 event_seq id 行");
          const value = decodeJsonSseData(data, frame);
          return validateMessageStreamEvent(value);
        },
        (event) => options.onEvent?.(event),
      ),
    },
  });
}

function validateMessageStreamEvent(value: unknown): MessageStreamEvent {
  if (!isRecord(value)) throw new Error("消息流事件必须是对象");
  const eventId = stringValue(value.event_id);
  const sessionId = stringValue(value.session_id);
  const turnId = stringValue(value.turn_id);
  const streamId = stringValue(value.turn_stream_id);
  const type = stringValue(value.type);
  const eventSeq = value.event_seq;
  if (!eventId || !sessionId || !turnId || !streamId || !type || !isMessageStreamEventType(type)) {
    throw new Error("消息流事件缺少合法的信封字段");
  }
  if (typeof eventSeq !== "number" || !Number.isInteger(eventSeq) || eventSeq < 0) {
    throw new Error("消息流 event_seq 必须是非负整数");
  }
  if (!isRecord(value.payload)) throw new Error("消息流 payload 必须是对象");
  return {
    event_id: eventId,
    session_id: sessionId,
    turn_id: turnId,
    turn_stream_id: streamId,
    event_seq: eventSeq,
    emitted_at: stringValue(value.emitted_at) ?? undefined,
    type,
    model_call_id: stringValue(value.model_call_id) ?? undefined,
    block_id: stringValue(value.block_id) ?? undefined,
    tool_execution_id: stringValue(value.tool_execution_id) ?? undefined,
    payload: value.payload,
  };
}

function isMessageStreamEventType(value: string): value is MessageStreamEventType {
  return new Set<MessageStreamEventType>([
    "stream.opened",
    "model.started",
    "model.completed",
    "model.retrying",
    "model.failed",
    "block.started",
    "block.delta",
    "block.completed",
    "tool_call",
    "tool_call.delta",
    "tool_call.completed",
    "tool.started",
    "tool.completed",
    "activity.started",
    "activity.updated",
    "activity.completed",
    "activity.failed",
    "interrupt.requested",
    "interrupt.rejected",
    "stream.completed",
    "stream.interrupted",
    "stream.failed",
    "stream.snapshot",
  ]).has(value as MessageStreamEventType);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}
