import type {
  AgentStateMessages,
  APIResponse,
  AttachmentRef,
  CursorPage,
  InterruptSessionResult,
  LLMRequestLogRecord,
  Message,
  MessageReplayAccepted,
  MessageReplayRequest,
  MessageRunAccepted,
  MessageRunRequest,
  DeliveryPolicy,
} from "../types/backend";
import {
  getApiBaseUrl,
  getGatewayToken,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./http";

export const DEFAULT_AGENT_ID = "default";

const AGENT_STATE_TIMEOUT_MS = 10000;
const SESSION_HISTORY_TIMEOUT_MS = 10000;

function normalizePageResult<T>(value: unknown): CursorPage<T> {
  if (!value || typeof value !== "object") {
    return { items: [] };
  }

  const record = value as {
    items?: T[];
    next_cursor?: string | null;
    has_more?: boolean;
  };
  return {
    items: Array.isArray(record.items) ? record.items : [],
    next_cursor: record.next_cursor ?? null,
    has_more:
      typeof record.has_more === "boolean" ? record.has_more : undefined,
  };
}

export async function getSessionAttachmentBlob(
  port: number,
  sessionId: string,
  fileId: string,
  workspaceId?: string | null,
  options: {
    variant?: "thumbnail" | "original";
    signal?: AbortSignal;
  } = {},
): Promise<Blob> {
  const localToken = await getGatewayToken(port);
  const query = new URLSearchParams({ file_id: fileId });
  query.set("variant", options.variant ?? "original");
  if (options.variant === "thumbnail") {
    query.set("max_edge", "384");
  }
  const response = await fetch(
    `${getApiBaseUrl(port)}/api/v1/sessions/${encodeURIComponent(sessionId)}/attachments/content?${query}`,
    {
      headers: {
        "X-Local-Token": localToken,
        ...workspaceHeader(workspaceId),
      },
      signal: options.signal,
    },
  );
  if (!response.ok) {
    const payload = await response.clone().json().catch(() => null) as {
      detail?: string;
    } | null;
    throw new Error(
      `读取消息附件失败: ${payload?.detail ?? `HTTP ${response.status}`}`,
    );
  }
  return response.blob();
}

export async function listMessages(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  options: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<CursorPage<Message>> {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit ?? 40));
  if (options.cursor) {
    params.set("cursor", options.cursor);
  }
  const data = await requestJson<APIResponse<CursorPage<Message>>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages?${params.toString()}`,
    {
      headers: workspaceHeader(workspaceId),
      timeoutMs: SESSION_HISTORY_TIMEOUT_MS,
      signal: options.signal,
    },
  );
  return normalizePageResult<Message>(unwrapApiData(data));
}

export async function getAgentStateMessages(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<AgentStateMessages> {
  const data = await requestJson<APIResponse<AgentStateMessages>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/agent-state/messages`,
    {
      headers: workspaceHeader(workspaceId),
      timeoutMs: AGENT_STATE_TIMEOUT_MS,
    },
  );
  return unwrapApiData(data);
}

export async function getLLMRequestLogs(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<LLMRequestLogRecord[]> {
  const data = await requestJson<APIResponse<LLMRequestLogRecord[]>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/llm-request-logs`,
    { headers: workspaceHeader(workspaceId) },
  );
  return unwrapApiData(data);
}

export async function sendMessage(
  port: number,
  sessionId: string,
  payload: MessageRunRequest,
  workspaceId?: string | null,
): Promise<MessageRunAccepted> {
  const accepted = unwrapApiData(
    await requestJson<APIResponse<MessageRunAccepted>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
  if (typeof accepted.message_id !== "string" || !accepted.message_id) {
    throw new Error("发送消息响应缺少 message_id");
  }
  if (typeof accepted.job_id !== "string" || !accepted.job_id) {
    throw new Error("发送消息响应缺少 job_id");
  }
  return accepted;
}

export async function sendUserMessage(
  port: number,
  sessionId: string,
  content: string,
  agentId: string = DEFAULT_AGENT_ID,
  attachments: AttachmentRef[] = [],
  workspaceId?: string | null,
  deliveryPolicy: DeliveryPolicy = "after_turn",
): Promise<MessageRunAccepted> {
  const payload: MessageRunRequest = {
    message: {
      role: "user",
      content,
      attachments,
      metadata: {},
    },
    run: {
      mode: "single_agent",
      agent_id: agentId,
      response_mode: "stream",
      async: true,
      delivery_policy: deliveryPolicy,
    },
  };

  return sendMessage(port, sessionId, payload, workspaceId);
}

export async function replayMessageTurn(
  port: number,
  sessionId: string,
  messageId: string,
  payload: MessageReplayRequest,
  workspaceId?: string | null,
): Promise<MessageReplayAccepted> {
  return unwrapApiData(
    await requestJson<APIResponse<MessageReplayAccepted>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/replay`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function interruptSession(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<InterruptSessionResult> {
  return unwrapApiData(
    await requestJson<APIResponse<InterruptSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: "POST", headers: workspaceHeader(workspaceId) },
    ),
  );
}
