import { API_PREFIX, DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_TOKEN } from './constants.js';

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

function parseSseBlock(block) {
  let eventType = 'message';
  let eventId = '';
  const dataLines = [];

  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) {
      continue;
    }

    if (line.startsWith('event:')) {
      eventType = line.slice(6).trim();
      continue;
    }

    if (line.startsWith('id:')) {
      eventId = line.slice(3).trim();
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

  let payload = data;
  try {
    payload = JSON.parse(data);
  } catch {
    // keep raw text payload
  }

  if (payload && typeof payload === 'object') {
    return {
      eventType: typeof payload.type === 'string' ? payload.type : eventType,
      eventId: typeof payload.event_id === 'string' ? payload.event_id : eventId,
      payload: payload.payload && typeof payload.payload === 'object' ? payload.payload : {},
      event: payload,
    };
  }

  return { eventType, eventId, payload };
}

export class TraceCursorGoneError extends Error {
  constructor(eventId) {
    super(`Trace 事件游标已失效: ${eventId}`);
    this.name = 'TraceCursorGoneError';
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

  if (!response.body) {
    throw new Error('后端事件流不可用');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
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
        if (!parsed) {
          continue;
        }

        onEvent?.(parsed);
      }

      if (done) {
        break;
      }
    }
  } catch (error) {
    if (signal?.aborted) {
      return;
    }

    onError?.(error);
    throw error;
  } finally {
    reader.releaseLock();
  }
}
