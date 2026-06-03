import type { ActiveJob, Message, Session, TraceEvent } from '../types/backend';

export interface WorkspaceInfo {
  root_path: string;
  name: string;
}

export interface PageResult<T> {
  items: T[];
}

export interface SessionAcceptResult {
  job_id: string | null;
  message_id: string | null;
}

export interface StreamEvent<TPayload = Record<string, unknown>> {
  eventType: string;
  payload: TPayload;
}

export const DEFAULT_BACKEND_HOST = '127.0.0.1';
export const DEFAULT_BACKEND_PORT = 8000;
export const DEFAULT_BACKEND_TOKEN = '';
export const DEFAULT_AGENT_ID = 'default';
export const DEFAULT_SESSION_TITLE = '新会话';

function normalizePageResult<T>(value: unknown): PageResult<T> {
  if (!value || typeof value !== 'object') {
    return { items: [] };
  }

  const record = value as { items?: T[] };
  return { items: Array.isArray(record.items) ? record.items : [] };
}

function getBaseUrl(port: number): string {
  return `http://${DEFAULT_BACKEND_HOST}:${port}`;
}

async function requestJson<T>(port: number, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getBaseUrl(port)}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    throw new Error(`请求失败 ${response.status} ${response.statusText}: ${path}`);
  }

  return await response.json() as T;
}

export async function getWorkspace(port: number): Promise<WorkspaceInfo> {
  return await requestJson<WorkspaceInfo>(port, '/api/v1/workspace');
}

export async function listAgents(port: number): Promise<unknown[]> {
  const data = await requestJson<unknown>(port, '/api/v1/agents');
  return Array.isArray(data) ? data : normalizePageResult<unknown>(data).items;
}

export async function listSessions(port: number): Promise<PageResult<Session>> {
  return normalizePageResult<Session>(await requestJson<unknown>(port, '/api/v1/sessions'));
}

export async function createSession(port: number, title: string = DEFAULT_SESSION_TITLE): Promise<Session> {
  return await requestJson<Session>(port, '/api/v1/sessions', {
    method: 'POST',
    body: JSON.stringify({ title }),
  });
}

export async function listMessages(port: number, sessionId: string): Promise<PageResult<Message>> {
  return normalizePageResult<Message>(await requestJson<unknown>(port, `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`));
}

export async function sendMessage(port: number, sessionId: string, payload: unknown): Promise<SessionAcceptResult> {
  return await requestJson<SessionAcceptResult>(port, `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getJob(port: number, jobId: string): Promise<ActiveJob | null> {
  return await requestJson<ActiveJob | null>(port, `/api/v1/jobs/${encodeURIComponent(jobId)}`);
}

export async function getSessionTraces(port: number, sessionId: string): Promise<TraceEvent[]> {
  const result = await requestJson<unknown>(port, `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces`);
  return Array.isArray(result) ? result as TraceEvent[] : [];
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
  const response = await fetch(url, { signal: options?.signal });

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
