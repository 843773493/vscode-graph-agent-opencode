import { API_PREFIX, DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_TOKEN } from './constants.js';
import {
  consumeSseResponse,
  decodeJsonSseData,
  defineSseEvent,
} from './sse.js';
import {
  validateSessionExecutionSse,
  validateTraceEvent,
} from './sseRuntime.js';

function buildUrl(port, path) {
  return `http://${DEFAULT_BACKEND_HOST}:${port}${API_PREFIX}${path}`;
}

async function requestJson(port, path, options = {}) {
  const url = buildUrl(port, path);
  console.log(`[API Request] ${options.method || 'GET'} ${url}`); // 添加日志

  const response = await fetch(url, {
    ...options,
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      ...(options.headers ?? {}),
    },
  });

  const responseText = await response.text(); // 提前读取响应体
  console.log(`[API Response] ${response.status} ${response.statusText}`, responseText.slice(0, 200)); // 打印响应摘要

  if (!response.ok) {
    throw new Error(`后端请求失败 ${response.status}: ${responseText}`);
  }

  try {
    return JSON.parse(responseText);
  } catch (e) {
    throw new Error(`响应解析失败: ${responseText.slice(0, 100)}`);
  }
}

export class TraceCursorGoneError extends Error {
  constructor(eventId) {
    super(`Trace 事件游标已失效: ${eventId}`);
    this.name = 'TraceCursorGoneError';
    this.eventId = eventId;
    this.status = 410;
  }
}

export class JobEventCursorGoneError extends Error {
  constructor(eventId) {
    super(`Job 事件游标已失效: ${eventId}`);
    this.name = 'JobEventCursorGoneError';
    this.eventId = eventId;
    this.status = 410;
  }
}

export async function getWorkspace(port) {
  const result = await requestJson(port, '/workspace');
  return result.data;
}

export async function listAgents(port) {
  const result = await requestJson(port, '/agents');
  return result.data ?? [];
}

export async function listSessions(port) {
  const result = await requestJson(port, '/sessions?limit=20');
  return result.data ?? { items: [] };
}

export async function createSession(port, title = '新会话') {
  const result = await requestJson(port, '/sessions', {
    method: 'POST',
    body: JSON.stringify({ title }),
  });

  return result.data;
}

export async function updateSession(port, sessionId, payload) {
  const result = await requestJson(port, `/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  return result.data;
}

export async function getSession(port, sessionId) {
  const result = await requestJson(port, `/sessions/${encodeURIComponent(sessionId)}`);
  return result.data;
}

export async function getSessionGoal(port, sessionId) {
  const result = await requestJson(
    port,
    `/sessions/${encodeURIComponent(sessionId)}/goal`,
  );
  return result.data ?? null;
}

export async function updateSessionGoal(port, sessionId, payload) {
  const result = await requestJson(
    port,
    `/sessions/${encodeURIComponent(sessionId)}/goal`,
    {
      method: 'PUT',
      body: JSON.stringify(payload),
    },
  );
  return result.data;
}

export async function clearSessionGoal(port, sessionId) {
  const result = await requestJson(
    port,
    `/sessions/${encodeURIComponent(sessionId)}/goal`,
    { method: 'DELETE' },
  );
  return result.data;
}

export async function moveSessionParent(port, sessionId, parentNodeId) {
  await requestJson(
    port,
    `/session-catalog/nodes/${encodeURIComponent(sessionId)}/parent`,
    {
      method: 'PATCH',
      body: JSON.stringify({ parent_node_id: parentNodeId }),
    },
  );
  return getSession(port, sessionId);
}

export async function listMessages(port, sessionId) {
  const result = await requestJson(port, `/sessions/${encodeURIComponent(sessionId)}/messages?limit=100`);
  return result.data ?? { items: [] };
}

export async function sendMessage(port, sessionId, payload) {
  const result = await requestJson(port, `/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });

  return result.data;
}

export async function getJob(port, jobId) {
  const result = await requestJson(port, `/jobs/${encodeURIComponent(jobId)}`);
  return result.data;
}

export async function getSessionTraces(port, sessionId, afterEventId = null) {
  const query = afterEventId
    ? `?${new URLSearchParams({ after_event_id: afterEventId }).toString()}`
    : '';
  const result = await requestJson(port, `/sessions/${encodeURIComponent(sessionId)}/traces${query}`);
  return result.data ?? [];
}

export async function streamSessionEvents(port, sessionId, { afterEventId, onEvent, onError, signal } = {}) {
  const response = await fetch(buildUrl(port, `/sessions/${encodeURIComponent(sessionId)}/traces/stream`), {
    headers: {
      accept: 'text/event-stream',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      ...(afterEventId ? { 'Last-Event-ID': afterEventId } : {}),
    },
    signal,
  });

  if (response.status === 410) {
    throw new TraceCursorGoneError(afterEventId ?? '');
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`后端事件流失败 ${response.status}: ${text}`);
  }

  try {
    await consumeSseResponse(response, {
      signal,
      idleTimeoutMs: 45_000,
      events: {
        trace: defineSseEvent(
          (data, frame) => {
            if (!frame.id) {
              throw new Error('SSE trace 缺少 id 行');
            }
            const event = validateTraceEvent(decodeJsonSseData(data, frame));
            if (event.event_id !== frame.id) {
              throw new Error(
                `SSE trace event_id 不一致: transport=${frame.id} payload=${event.event_id}`,
              );
            }
            return event;
          },
          (event) => {
            const raw = event.raw && typeof event.raw === 'object' ? event.raw : {};
            const payload = raw.payload && typeof raw.payload === 'object'
              ? raw.payload
              : {};
            onEvent?.({
              eventType: event.type,
              eventId: event.event_id,
              payload,
              event,
            });
          },
        ),
      },
    });
  } catch (error) {
    if (signal?.aborted) {
      return;
    }

    onError?.(error);
    throw error;
  }
}

export async function streamJobEvents(
  port,
  jobId,
  { afterEventId, onEvent, onError, signal } = {},
) {
  const response = await fetch(
    buildUrl(port, `/jobs/${encodeURIComponent(jobId)}/events/stream`),
    {
      headers: {
        accept: 'text/event-stream',
        'X-Local-Token': DEFAULT_BACKEND_TOKEN,
        ...(afterEventId ? { 'Last-Event-ID': afterEventId } : {}),
      },
      signal,
    },
  );
  if (response.status === 410) {
    throw new JobEventCursorGoneError(afterEventId ?? '');
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`后端 Job 事件流失败 ${response.status}: ${text}`);
  }

  try {
    await consumeSseResponse(response, {
      signal,
      idleTimeoutMs: 45_000,
      events: {
        '*': defineSseEvent(
          (data, frame) => {
            if (!frame.id) {
              throw new Error(`SSE Job 事件缺少 id 行: event=${frame.event}`);
            }
            const envelope = validateSessionExecutionSse(
              decodeJsonSseData(data, frame),
            );
            if (envelope.event.event_id !== frame.id) {
              throw new Error(
                `SSE Job event_id 不一致: transport=${frame.id} payload=${envelope.event.event_id}`,
              );
            }
            if (envelope.event.type !== frame.event) {
              throw new Error(
                `SSE Job event type 不一致: transport=${frame.event} payload=${envelope.event.type}`,
              );
            }
            return envelope;
          },
          (envelope) => onEvent?.({
            eventType: envelope.event.type,
            eventId: envelope.event.event_id,
            payload: envelope.event.payload,
            event: envelope.event,
            rawType: envelope.raw_type,
            rawPayload: envelope.raw_payload,
          }),
        ),
      },
    });
  } catch (error) {
    if (signal?.aborted) {
      return;
    }
    onError?.(error);
    throw error;
  }
}
