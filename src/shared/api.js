import { API_PREFIX, DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_TOKEN } from './constants.js';

function buildUrl(port, path) {
  return `http://${DEFAULT_BACKEND_HOST}:${port}${API_PREFIX}${path}`;
}

async function requestJson(port, path, options = {}) {
  const response = await fetch(buildUrl(port, path), {
    ...options,
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      ...(options.headers ?? {}),
    },
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`后端请求失败 ${response.status}: ${text}`);
  }

  return response.json();
}

function parseSseBlock(block) {
  let eventType = 'message';
  const dataLines = [];

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

  let payload = data;
  try {
    payload = JSON.parse(data);
  } catch {
    // keep raw text payload
  }

  return { eventType, payload };
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

export async function getSessionTraces(port, sessionId) {
  const result = await requestJson(port, `/sessions/${encodeURIComponent(sessionId)}/traces`);
  return result.data ?? [];
}

export async function streamJobEvents(port, jobId, { onEvent, onError, signal } = {}) {
  const response = await fetch(buildUrl(port, `/jobs/${encodeURIComponent(jobId)}/events/stream`), {
    headers: {
      accept: 'text/event-stream',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
    },
    signal,
  });

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
