export class HttpError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = "HttpError";
    this.statusCode = statusCode;
  }
}

export function jsonHeaders(extraHeaders = {}) {
  return {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, PUT, OPTIONS",
    "access-control-allow-headers": "content-type",
    "cache-control": "no-store",
    ...extraHeaders,
  };
}

export function writeJson(response, statusCode, payload, extraHeaders = {}) {
  response.writeHead(statusCode, jsonHeaders(extraHeaders));
  response.end(JSON.stringify(payload));
}

export async function readJsonRequest(request, maxBytes = 1024 * 1024) {
  const body = await readRawRequest(request, maxBytes);
  const text = body.toString("utf8");
  if (text.trim() === "") {
    throw new HttpError(400, "请求体不能为空");
  }

  try {
    return JSON.parse(text);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new HttpError(400, `请求体不是合法 JSON: ${message}`);
  }
}

export async function readRawRequest(request, maxBytes = 20 * 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) {
      throw new HttpError(413, `请求体过大，最大允许 ${maxBytes} bytes`);
    }
    chunks.push(chunk);
  }

  return Buffer.concat(chunks);
}

export function requireMethod(request, expectedMethod) {
  if (request.method !== expectedMethod) {
    throw new HttpError(405, `只支持 ${expectedMethod}，当前请求方法: ${request.method}`);
  }
}

export function requestUrl(request) {
  return new URL(request.url || "/", `http://${request.headers.host || "127.0.0.1"}`);
}
