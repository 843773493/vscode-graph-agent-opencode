import type {
  APIResponse,
  CursorPage,
  InterruptSessionResult,
  Message,
  MessageRunAccepted,
  MessageRunRequest,
  Session,
  TraceEvent,
  WorkspaceInfo,
} from './types/backend';

export const DEFAULT_BACKEND_HOST = '127.0.0.1';
export const DEFAULT_BACKEND_PORT = 8000;
export const DEFAULT_BACKEND_TOKEN = 'local-dev-token';
export const DEFAULT_AGENT_ID = 'default';
export const DEFAULT_SESSION_TITLE = '新会话';

function getBaseUrl(port: number): string {
  if (typeof window !== 'undefined' && window.location.port === '8001') {
    return '';
  }

  return `http://${DEFAULT_BACKEND_HOST}:${port}`;
}

async function requestJson<T>(port: number, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getBaseUrl(port)}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    throw new Error(`请求失败 ${response.status} ${response.statusText}: ${path}`);
  }

  return await response.json() as T;
}

function unwrapApiData<T>(response: APIResponse<T>): T {
  if (response.data == null) {
    throw new Error(`后端响应缺少 data 字段: ${response.message || 'unknown message'}`);
  }

  return response.data;
}

function normalizePageResult<T>(value: unknown): CursorPage<T> {
  if (!value || typeof value !== 'object') {
    return { items: [] };
  }

  const record = value as { items?: T[]; next_cursor?: string | null; has_more?: boolean };
  return {
    items: Array.isArray(record.items) ? record.items : [],
    next_cursor: record.next_cursor ?? null,
    has_more: typeof record.has_more === 'boolean' ? record.has_more : undefined,
  };
}

export async function getWorkspace(port: number): Promise<WorkspaceInfo> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceInfo>>(port, '/api/v1/workspace'));
}

export async function listSessions(port: number): Promise<CursorPage<Session>> {
  const data = await requestJson<APIResponse<CursorPage<Session>>>(port, '/api/v1/sessions');
  return normalizePageResult<Session>(unwrapApiData(data));
}

export async function createSession(port: number, title: string = DEFAULT_SESSION_TITLE): Promise<Session> {
  return unwrapApiData(await requestJson<APIResponse<Session>>(port, '/api/v1/sessions', {
    method: 'POST',
    body: JSON.stringify({ title }),
  }));
}

export async function listMessages(port: number, sessionId: string): Promise<CursorPage<Message>> {
  const data = await requestJson<APIResponse<CursorPage<Message>>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
  );
  return normalizePageResult<Message>(unwrapApiData(data));
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
        method: 'POST',
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
): Promise<MessageRunAccepted> {
  const payload: MessageRunRequest = {
    message: {
      role: 'user',
      content,
      attachments: [],
      metadata: {},
    },
    run: {
      mode: 'single_agent',
      agent_id: agentId,
      response_mode: 'stream',
      async: true,
    },
  };

  return sendMessage(port, sessionId, payload);
}

export async function interruptSession(port: number, sessionId: string): Promise<InterruptSessionResult> {
  return unwrapApiData(
    await requestJson<APIResponse<InterruptSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: 'POST' },
    ),
  );
}

export async function getSessionTraces(port: number, sessionId: string): Promise<TraceEvent[]> {
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
}

function parseSseBlock(block: string): SessionStreamEvent | null {
  let eventType = 'message';
  const dataLines: string[] = [];

  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) {
      continue;
    }

    if (line.startsWith('event:')) {
      eventType = line.slice(6).trim();
      continue;
    }

    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  const data = dataLines.join('\n');
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
      session_id: '',
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
      accept: 'text/event-stream',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
    },
  });

  if (!response.ok || !response.body) {
    throw new Error(`无法连接会话事件流: ${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });

      let boundaryIndex = buffer.indexOf('\n\n');
      while (boundaryIndex !== -1) {
        const block = buffer.slice(0, boundaryIndex).trim();
        buffer = buffer.slice(boundaryIndex + 2);
        boundaryIndex = buffer.indexOf('\n\n');

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
