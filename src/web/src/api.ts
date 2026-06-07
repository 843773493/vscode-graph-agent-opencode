import type { ActiveJob, Message, Session, TraceEvent } from './types/backend';

export interface WorkspaceInfo {
  root_path: string;
  name: string;
}

export interface APIResponse<T> {
  code: number;
  message: string;
  data: T | null;
  request_id?: string | null;
}

export interface PageResult<T> {
  items: T[];
  next_cursor?: string | null;
  has_more?: boolean;
}

export interface SessionAcceptResult {
  job_id: string | null;
  message_id: string | null;
}

export interface MessageRunRequest {
  message: {
    role: 'user' | 'assistant' | 'system';
    content: string;
    attachments: Array<Record<string, unknown>>;
    metadata: Record<string, unknown>;
  };
  run: {
    mode: 'single_agent' | 'multi_agent';
    agent_id: string | null;
    response_mode: string;
    async: boolean;
    max_steps: number;
    timeout_seconds: number;
    context: Record<string, unknown>;
  };
}

export interface StreamEvent<TPayload = Record<string, unknown>> {
  eventType: string;
  payload: TPayload;
}

export const DEFAULT_BACKEND_HOST = '127.0.0.1';
export const DEFAULT_BACKEND_PORT = 8000;
export const DEFAULT_BACKEND_TOKEN = 'local-dev-token';
export const DEFAULT_AGENT_ID = 'default';
export const DEFAULT_SESSION_TITLE = '新会话';

function normalizePageResult<T>(value: unknown): PageResult<T> {
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

function getBaseUrl(port: number): string {
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

export async function getWorkspace(port: number): Promise<WorkspaceInfo> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceInfo>>(port, '/api/v1/workspace'));
}

export async function listAgents(port: number): Promise<unknown[]> {
  const data = await requestJson<APIResponse<unknown[] | { items?: unknown[] }>>(port, '/api/v1/agents');
  const payload = unwrapApiData(data);
  return Array.isArray(payload) ? payload : normalizePageResult<unknown>(payload).items;
}

export async function listSessions(port: number): Promise<PageResult<Session>> {
  const data = await requestJson<APIResponse<{ items?: Session[]; next_cursor?: string | null; has_more?: boolean }>>(port, '/api/v1/sessions');
  return normalizePageResult<Session>(unwrapApiData(data));
}

export async function createSession(port: number, title: string = DEFAULT_SESSION_TITLE): Promise<Session> {
  return unwrapApiData(await requestJson<APIResponse<Session>>(port, '/api/v1/sessions', {
    method: 'POST',
    body: JSON.stringify({ title }),
  }));
}

export async function listMessages(port: number, sessionId: string): Promise<PageResult<Message>> {
  const data = await requestJson<APIResponse<{ items?: Message[]; next_cursor?: string | null; has_more?: boolean }>>(port, `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`);
  return normalizePageResult<Message>(unwrapApiData(data));
}

export async function sendMessage(port: number, sessionId: string, payload: unknown): Promise<SessionAcceptResult> {
  return unwrapApiData(await requestJson<APIResponse<SessionAcceptResult>>(port, `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(payload),
  }));
}

export async function sendUserMessage(
  port: number,
  sessionId: string,
  content: string,
  agentId: string = DEFAULT_AGENT_ID,
): Promise<SessionAcceptResult> {
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
      max_steps: 20,
      timeout_seconds: 600,
      context: {},
    },
  };

  return sendMessage(port, sessionId, payload);
}

export async function getJob(port: number, jobId: string): Promise<ActiveJob | null> {
  return unwrapApiData(await requestJson<APIResponse<ActiveJob | null>>(port, `/api/v1/jobs/${encodeURIComponent(jobId)}`));
}

export async function getSessionTraces(port: number, sessionId: string): Promise<TraceEvent[]> {
  const result = await requestJson<APIResponse<TraceEvent[] | { items?: TraceEvent[] }>>(port, `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces`);
  const payload = unwrapApiData(result);
  return Array.isArray(payload) ? payload as TraceEvent[] : normalizePageResult<TraceEvent>(payload).items;
}

export async function streamJobEvents(
  port: number,
  jobId: string,
  options?: {
    onEvent?: (event: StreamEvent) => void;
    onError?: (error: unknown) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  const url = `${getBaseUrl(port)}/api/v1/jobs/${encodeURIComponent(jobId)}/events`;
  const response = await fetch(url, {
    signal: options?.signal,
    headers: {
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
    },
  });

  if (!response.ok || !response.body) {
    throw new Error(`无法连接事件流: ${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    let lineBreakIndex = buffer.indexOf('\n');
    while (lineBreakIndex !== -1) {
      const line = buffer.slice(0, lineBreakIndex).trim();
      buffer = buffer.slice(lineBreakIndex + 1);
      lineBreakIndex = buffer.indexOf('\n');

      if (!line || line.startsWith(':')) {
        continue;
      }

      if (line.startsWith('data:')) {
        const raw = line.slice(5).trim();
        if (!raw) {
          continue;
        }

        try {
          const parsed = JSON.parse(raw) as StreamEvent;
          options?.onEvent?.(parsed);
        } catch (error) {
          options?.onError?.(error);
        }
      }
    }
  }
}
