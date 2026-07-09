import type {
  Agent,
  AgentStateMessages,
  APIResponse,
  AttachmentRef,
  CursorPage,
  DeleteSessionResult,
  InterruptSessionResult,
  LLMRequestLogRecord,
  Message,
  MessageRunAccepted,
  MessageRunRequest,
  Session,
  SessionCompactResult,
  SessionResourceAction,
  SessionResourceControlResult,
  SessionResourceKind,
  SessionResourceList,
  SessionUpdateRequest,
  TraceEvent,
  WorkspaceFileContent,
  WorkspaceFileList,
  WorkspaceInfo,
} from "./types/backend";

export const DEFAULT_BACKEND_HOST = "127.0.0.1";
export const DEFAULT_BACKEND_PORT = 8014;
export const DEFAULT_BACKEND_TOKEN = "local-dev-token";
export const DEFAULT_AGENT_ID = "default";
export const DEFAULT_SESSION_TITLE = "新会话";
const AGENT_STATE_TIMEOUT_MS = 10000;

type RequestJsonInit = RequestInit & {
  timeoutMs?: number;
};

function getBaseUrl(port: number): string {
  if (typeof window !== "undefined" && window.location.port !== String(port)) {
    return "";
  }

  return `http://${DEFAULT_BACKEND_HOST}:${port}`;
}

export async function requestJson<T>(
  port: number,
  path: string,
  init?: RequestJsonInit,
): Promise<T> {
  const { timeoutMs, signal, ...fetchInit } = init ?? {};
  const controller = timeoutMs ? new AbortController() : null;
  const timeoutErrorMessage = `请求超时: ${path}`;
  let timeoutId: number | null = null;
  const timeoutPromise = timeoutMs
    ? new Promise<Response>((_, reject) => {
        timeoutId = window.setTimeout(() => {
          controller?.abort();
          reject(new Error(timeoutErrorMessage));
        }, timeoutMs);
      })
    : null;

  try {
    const fetchPromise = fetch(`${getBaseUrl(port)}${path}`, {
      headers: {
        "Content-Type": "application/json",
        "X-Local-Token": DEFAULT_BACKEND_TOKEN,
        ...(fetchInit.headers ?? {}),
      },
      ...fetchInit,
      signal: signal ?? controller?.signal,
    });
    const response = timeoutPromise
      ? await Promise.race([fetchPromise, timeoutPromise])
      : await fetchPromise;

    if (!response.ok) {
      throw new Error(
        `请求失败 ${response.status} ${response.statusText}: ${path}`,
      );
    }

    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof Error && error.message === timeoutErrorMessage) {
      throw error;
    }
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(timeoutErrorMessage);
    }
    throw error;
  } finally {
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId);
    }
  }
}

export function unwrapApiData<T>(response: APIResponse<T>): T {
  if (response.data == null) {
    throw new Error(
      `后端响应缺少 data 字段: ${response.message || "unknown message"}`,
    );
  }

  return response.data;
}

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

export async function getWorkspace(port: number): Promise<WorkspaceInfo> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceInfo>>(port, "/api/v1/workspace"),
  );
}

export async function getWorkspaceFiles(
  port: number,
  path: string = "",
): Promise<WorkspaceFileList> {
  const query = new URLSearchParams();
  if (path) {
    query.set("path", path);
  }

  const suffix = query.toString();
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceFileList>>(
      port,
      `/api/v1/workspace/files${suffix ? `?${suffix}` : ""}`,
    ),
  );
}

export async function getWorkspaceFileContent(
  port: number,
  path: string,
): Promise<WorkspaceFileContent> {
  const query = new URLSearchParams({ path });
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceFileContent>>(
      port,
      `/api/v1/workspace/files/content?${query.toString()}`,
    ),
  );
}

export async function listSessions(port: number): Promise<CursorPage<Session>> {
  const data = await requestJson<APIResponse<CursorPage<Session>>>(
    port,
    "/api/v1/sessions",
  );
  return normalizePageResult<Session>(unwrapApiData(data));
}

export async function getSession(
  port: number,
  sessionId: string,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    ),
  );
}

export async function listAgents(port: number): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(port, "/api/v1/agents"),
  );
}

export async function createSession(
  port: number,
  title: string = DEFAULT_SESSION_TITLE,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(port, "/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  );
}

export async function updateSession(
  port: number,
  sessionId: string,
  payload: SessionUpdateRequest,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function deleteSession(
  port: number,
  sessionId: string,
): Promise<DeleteSessionResult> {
  return unwrapApiData(
    await requestJson<APIResponse<DeleteSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    ),
  );
}

export function updateSessionAgent(
  port: number,
  sessionId: string,
  agentId: string,
): Promise<Session> {
  return updateSession(port, sessionId, { agent_id: agentId });
}

export async function compactSessionContext(
  port: number,
  sessionId: string,
): Promise<SessionCompactResult> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionCompactResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/compact`,
      { method: "POST" },
    ),
  );
}

export async function listMessages(
  port: number,
  sessionId: string,
): Promise<CursorPage<Message>> {
  const data = await requestJson<APIResponse<CursorPage<Message>>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
  );
  return normalizePageResult<Message>(unwrapApiData(data));
}

export async function getAgentStateMessages(
  port: number,
  sessionId: string,
): Promise<AgentStateMessages> {
  const data = await requestJson<APIResponse<AgentStateMessages>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/agent-state/messages`,
    { timeoutMs: AGENT_STATE_TIMEOUT_MS },
  );
  return unwrapApiData(data);
}

export async function getLLMRequestLogs(
  port: number,
  sessionId: string,
): Promise<LLMRequestLogRecord[]> {
  const data = await requestJson<APIResponse<LLMRequestLogRecord[]>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/llm-request-logs`,
  );
  return unwrapApiData(data);
}

export async function getSessionResources(
  port: number,
  sessionId: string,
): Promise<SessionResourceList> {
  const data = await requestJson<APIResponse<SessionResourceList>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/resources`,
  );
  return unwrapApiData(data);
}

export async function controlSessionResource(
  port: number,
  sessionId: string,
  kind: SessionResourceKind,
  resourceId: string,
  action: SessionResourceAction,
): Promise<SessionResourceControlResult> {
  const data = await requestJson<APIResponse<SessionResourceControlResult>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/resources/${encodeURIComponent(kind)}/${encodeURIComponent(resourceId)}/control`,
    {
      method: "POST",
      body: JSON.stringify({ action }),
    },
  );
  return unwrapApiData(data);
}

export async function sendMessage(
  port: number,
  sessionId: string,
  payload: MessageRunRequest,
): Promise<MessageRunAccepted> {
  return unwrapApiData(
    await requestJson<APIResponse<MessageRunAccepted>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function sendUserMessage(
  port: number,
  sessionId: string,
  content: string,
  agentId: string = DEFAULT_AGENT_ID,
  attachments: AttachmentRef[] = [],
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
    },
  };

  return sendMessage(port, sessionId, payload);
}

export async function interruptSession(
  port: number,
  sessionId: string,
): Promise<InterruptSessionResult> {
  return unwrapApiData(
    await requestJson<APIResponse<InterruptSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: "POST" },
    ),
  );
}

export async function getSessionTraces(
  port: number,
  sessionId: string,
): Promise<TraceEvent[]> {
  const result = await requestJson<APIResponse<TraceEvent[]>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces`,
  );
  return unwrapApiData(result);
}

export interface SessionStreamEvent {
  event_id: string;
  session_id: string;
  job_id: string | null;
  step_id: string | null;
  agent_id: string | null;
  timestamp: string;
  type: string;
  payload: Record<string, unknown>;
  /** 后端 DTO 格式可能将真实事件数据嵌套在 raw 中 */
  raw?: Record<string, unknown>;
}

function parseSseBlock(block: string): SessionStreamEvent | null {
  let eventType = "message";
  const dataLines: string[] = [];

  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) {
      continue;
    }

    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
      continue;
    }

    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  const data = dataLines.join("\n");
  if (!data) {
    return null;
  }

  try {
    // 后端 SSE 的 event 行始终为 "trace"，真实事件类型在 JSON payload.type 中
    const payload = JSON.parse(data) as SessionStreamEvent;
    // 如果 payload.type 不存在（旧格式），则回退到 eventType
    if (!payload.type) {
      payload.type = eventType;
    }
    return payload;
  } catch {
    return {
      event_id: `raw_${Date.now()}`,
      session_id: "",
      job_id: null,
      step_id: null,
      agent_id: null,
      timestamp: new Date().toISOString(),
      type: eventType,
      payload: { raw: data },
    };
  }
}

export async function streamSessionEvents(
  port: number,
  sessionId: string,
  options?: {
    onEvent?: (event: SessionStreamEvent) => void;
    onError?: (error: unknown) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  const url = `${getBaseUrl(port)}/api/v1/sessions/${encodeURIComponent(sessionId)}/traces/stream`;
  const response = await fetch(url, {
    signal: options?.signal,
    headers: {
      accept: "text/event-stream",
      "X-Local-Token": DEFAULT_BACKEND_TOKEN,
    },
  });

  if (!response.ok || !response.body) {
    throw new Error(
      `无法连接会话事件流: ${response.status} ${response.statusText}`,
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });

      let boundaryIndex = buffer.indexOf("\n\n");
      while (boundaryIndex !== -1) {
        const block = buffer.slice(0, boundaryIndex).trim();
        buffer = buffer.slice(boundaryIndex + 2);
        boundaryIndex = buffer.indexOf("\n\n");

        if (!block) {
          continue;
        }

        const parsed = parseSseBlock(block);
        if (parsed) {
          options?.onEvent?.(parsed);
        }
      }

      if (done) {
        break;
      }
    }
  } catch (error) {
    if (options?.signal?.aborted) {
      return;
    }

    options?.onError?.(error);
    throw error;
  } finally {
    reader.releaseLock();
  }
}
