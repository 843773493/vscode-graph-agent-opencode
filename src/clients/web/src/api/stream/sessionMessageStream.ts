import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from "../../sse/sseClient";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import {
  HttpRequestError,
  requestGatewayResponse,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "../http";
import type {
  MessageStreamEvent,
  MessageStreamEventType,
} from "../../state/messageStream/index";
// 复用消息流原语中心的值归一实现，不再在 api 层维护第二份 stringValue。
import { stringValue } from "../../state/messageStream/state";
import type { APIResponse } from "../../types/backend";
import { isRecord } from "../../utils/jsonDisplay";
import {
  validateMessageStreamSnapshot,
  validateMessageStreamSnapshotPayload,
  type MessageStreamSnapshotResponse,
} from "./messageStreamSnapshot";

export type { MessageStreamSnapshotResponse } from "./messageStreamSnapshot";

export class MessageStreamCursorGoneError extends Error {
  readonly status = 410;

  constructor(readonly afterSeq: number) {
    super(`消息流 event_seq 游标已失效: ${afterSeq}`);
    this.name = "MessageStreamCursorGoneError";
  }
}

export class MessageStreamConnectionError extends Error {
  readonly retryable = true;

  constructor(
    readonly status: number,
    readonly statusText: string,
  ) {
    super(`无法连接 Turn 消息流: ${status} ${statusText}`);
    this.name = "MessageStreamConnectionError";
  }
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
    return validateMessageStreamSnapshot(unwrapApiData(response));
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
  // SSE 长期连接只共享统一凭据与刷新重试；生命周期内的断线重连由调用方负责。
  let response: Response;
  try {
    response = await requestGatewayResponse(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/message-stream?${params.toString()}`,
      {
        signal: options.signal,
        skipGatewayUserSession: true,
        headers: {
          accept: "text/event-stream",
          ...workspaceHeader(options.workspaceId),
          ...(options.afterSeq !== undefined
            ? { "Last-Event-ID": String(options.afterSeq) }
            : {}),
        },
      },
    );
  } catch (error) {
    if (error instanceof HttpRequestError && error.status === 410) {
      throw new MessageStreamCursorGoneError(options.afterSeq ?? 0);
    }
    throw error;
  }
  if (!response.body) {
    throw new MessageStreamConnectionError(response.status, response.statusText);
  }
  options.onConnected?.(response.headers.get("X-Message-Stream-ID"));
  await consumeSseResponse(response, {
    signal: options.signal,
    // 服务端每 15s 发一次 `: heartbeat` 注释；阈值必须大于该间隔，否则健康空闲
    // 流会被误判断线。达到阈值即由 sse.js 统一抛错，绝不让页面在无字节连接上
    // 无限静默挂起。
    idleTimeoutMs: SSE_IDLE_TIMEOUT_MS,
    onActivity: options.onActivity,
    yieldBetweenEvents: true,
    events: {
      "*": defineSseEvent(
        (data, frame) => {
          if (!frame.id) throw new Error("SSE 消息流缺少 event_seq id 行");
          return validateMessageStreamEvent(decodeJsonSseData(data, frame));
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
  for (const field of [
    "model_call_id",
    "block_id",
    "tool_call_id",
    "tool_invocation_id",
    "tool_attempt_id",
    "tool_execution_id",
    "workspace_id",
  ] as const) {
    const envelopeValue = value[field];
    const payloadValue = value.payload[field];
    if (envelopeValue !== undefined && envelopeValue !== null && !stringValue(envelopeValue)) {
      throw new Error(`消息流 ${field} 信封身份必须是非空字符串`);
    }
    if (payloadValue !== undefined && payloadValue !== null && !stringValue(payloadValue)) {
      throw new Error(`消息流 ${field} payload 身份必须是非空字符串`);
    }
    const envelopeId = stringValue(envelopeValue);
    const payloadId = stringValue(payloadValue);
    if (envelopeId && payloadId && envelopeId !== payloadId) {
      throw new Error(`消息流 ${field} 信封与 payload 身份不一致`);
    }
  }
  const envelope = {
    event_id: eventId,
    session_id: sessionId,
    turn_id: turnId,
    turn_stream_id: streamId,
    event_seq: eventSeq,
    emitted_at: stringValue(value.emitted_at) ?? undefined,
    workspace_id: stringValue(value.workspace_id) ?? undefined,
    model_call_id: stringValue(value.model_call_id) ?? undefined,
    block_id: stringValue(value.block_id) ?? undefined,
    tool_execution_id: stringValue(value.tool_execution_id) ?? undefined,
    tool_call_id: stringValue(value.tool_call_id) ?? undefined,
    tool_invocation_id: stringValue(value.tool_invocation_id) ?? undefined,
    tool_attempt_id: stringValue(value.tool_attempt_id) ?? undefined,
  };
  if (type === "stream.snapshot") {
    const snapshot = validateMessageStreamSnapshotPayload(value.payload);
    if (snapshot.snapshot_seq !== eventSeq) {
      throw new Error("消息流 snapshot_seq 必须与事件 event_seq 一致");
    }
    return { ...envelope, type, payload: snapshot };
  }
  return { ...envelope, type, payload: value.payload };
}

// 事件类型白名单的唯一运行时清单。逐字对齐 types.ts 的 MessageStreamEventType 联合。
const MESSAGE_STREAM_EVENT_TYPES = [
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
] as const satisfies readonly MessageStreamEventType[];

// 双向编译期穷尽守卫：类型别名必须被真正消费，tsc 才会例化它，否则守卫形同虚设。
// 下面一行把它从 "未消费的别名" 变成 "被赋值的常量"，从而在以下任一情况下报错：
// - 联合类型新增成员而白名单未跟上（Exclude 结果非 never，别名为 never，赋值 true 报错）；
// - 白名单漏掉任一联合成员（同理报错）。
// 白名单多出联合之外的成员由上面的 satisfies 拦截。
type MessageStreamEventTypesExhaustive = Exclude<
  MessageStreamEventType,
  (typeof MESSAGE_STREAM_EVENT_TYPES)[number]
> extends never ? true : never;

const MESSAGE_STREAM_EVENT_TYPES_EXHAUSTIVE: MessageStreamEventTypesExhaustive = true;

const MESSAGE_STREAM_EVENT_TYPE_SET: ReadonlySet<string> = new Set(MESSAGE_STREAM_EVENT_TYPES);

function isMessageStreamEventType(value: string): value is MessageStreamEventType {
  return MESSAGE_STREAM_EVENT_TYPE_SET.has(value);
}
