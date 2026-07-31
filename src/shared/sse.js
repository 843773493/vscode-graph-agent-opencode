/**
 * 标准 SSE 帧解析与流消费器。这里只处理传输语义，不解释业务 DTO。
 */

export function defineSseEvent(decode, handle) {
  return { decode, handle };
}

export function decodeJsonSseData(data, frame) {
  try {
    return JSON.parse(data);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `SSE JSON 无法解析: event=${frame.event} id=${frame.id ?? ''} error=${detail}`,
    );
  }
}

export function parseSseFrameBlock(block) {
  let event = 'message';
  let id = null;
  const dataLines = [];
  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) {
      continue;
    }
    const separatorIndex = line.indexOf(':');
    const field = separatorIndex === -1 ? line : line.slice(0, separatorIndex);
    let value = separatorIndex === -1 ? '' : line.slice(separatorIndex + 1);
    if (value.startsWith(' ')) {
      value = value.slice(1);
    }
    if (field === 'event') {
      event = value || 'message';
    } else if (field === 'id' && !value.includes('\0')) {
      id = value;
    } else if (field === 'data') {
      dataLines.push(value);
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  return { event, id, data: dataLines.join('\n') };
}

function readStreamChunk(reader, { idleTimeoutMs, idleTimeoutError, signal }) {
  if (idleTimeoutMs !== undefined && idleTimeoutMs <= 0) {
    throw new Error(`SSE 空闲超时必须大于 0: ${idleTimeoutMs}`);
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let timeoutId;
    const finish = (callback, value) => {
      if (settled) {
        return;
      }
      settled = true;
      if (timeoutId !== undefined) {
        globalThis.clearTimeout(timeoutId);
      }
      signal?.removeEventListener('abort', handleAbort);
      callback(value);
    };
    const handleAbort = () => finish(
      reject,
      new DOMException('SSE 读取已中止', 'AbortError'),
    );

    if (signal?.aborted) {
      handleAbort();
      return;
    }
    signal?.addEventListener('abort', handleAbort, { once: true });
    if (idleTimeoutMs !== undefined) {
      timeoutId = globalThis.setTimeout(() => {
        finish(
          reject,
          idleTimeoutError?.(idleTimeoutMs)
            ?? new Error(`SSE 超过 ${idleTimeoutMs}ms 未收到任何数据`),
        );
      }, idleTimeoutMs);
    }
    void reader.read().then(
      (result) => finish(resolve, result),
      (error) => finish(reject, error),
    );
  });
}

function assertEventStreamResponse(response) {
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
  if (!contentType.includes('text/event-stream')) {
    throw new Error(
      `SSE 响应 Content-Type 错误: ${contentType || '<missing>'}`,
    );
  }
  if (!response.body) {
    throw new Error('SSE 响应缺少可读 body');
  }
}

export async function consumeSseResponse(response, options) {
  assertEventStreamResponse(response);
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let pendingCarriageReturn = false;
  let reachedEnd = false;

  const dispatchBlock = (block) => {
    const frame = parseSseFrameBlock(block);
    if (!frame) {
      return;
    }
    const definition = options.events[frame.event] ?? options.events['*'];
    if (!definition) {
      throw new Error(`未注册的 SSE 事件类型: ${frame.event}`);
    }
    definition.handle(definition.decode(frame.data, frame), frame);
  };

  try {
    while (true) {
      const { done, value } = await readStreamChunk(reader, options);
      if (value && value.byteLength > 0) {
        options.onActivity?.();
      }
      let decoded = decoder.decode(value ?? new Uint8Array(), { stream: !done });
      if (pendingCarriageReturn) {
        decoded = `\r${decoded}`;
        pendingCarriageReturn = false;
      }
      if (!done && decoded.endsWith('\r')) {
        decoded = decoded.slice(0, -1);
        pendingCarriageReturn = true;
      }
      if (done && pendingCarriageReturn) {
        decoded += '\r';
        pendingCarriageReturn = false;
      }
      buffer += decoded.replace(/\r\n|\r/g, '\n');

      let boundaryIndex = buffer.indexOf('\n\n');
      while (boundaryIndex !== -1) {
        dispatchBlock(buffer.slice(0, boundaryIndex));
        buffer = buffer.slice(boundaryIndex + 2);
        boundaryIndex = buffer.indexOf('\n\n');
      }
      if (done) {
        reachedEnd = true;
        if (buffer.trim()) {
          dispatchBlock(buffer);
        }
        return;
      }
    }
  } catch (error) {
    if (options.signal?.aborted) {
      return;
    }
    throw error;
  } finally {
    if (!reachedEnd) {
      await reader.cancel();
    }
    reader.releaseLock();
  }
}
