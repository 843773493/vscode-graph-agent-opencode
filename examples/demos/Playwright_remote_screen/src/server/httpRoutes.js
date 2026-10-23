import { readFile } from "node:fs/promises";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";
import { FIXED_BROWSER_ID } from "./protocol.js";
import { executeBrowserCommand, parseBrowserControlCommand } from "./browserControlCommands.js";

const clientRoot = fileURLToPath(new URL("../client/", import.meta.url));
const MAX_COMMAND_BODY_BYTES = 65536;
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);
const DECODED_BODY_HEADERS = new Set(["content-encoding", "content-length"]);

class HttpRequestError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
  }
}

function mimeType(filePath) {
  const ext = path.extname(filePath);
  if (ext === ".html") return "text/html; charset=utf-8";
  if (ext === ".js") return "text/javascript; charset=utf-8";
  if (ext === ".css") return "text/css; charset=utf-8";
  if (ext === ".json") return "application/json; charset=utf-8";
  if (ext === ".svg") return "image/svg+xml";
  return "application/octet-stream";
}

function resolveStaticPath(requestUrl) {
  const url = new URL(requestUrl || "/", "http://127.0.0.1");
  if (url.pathname === "/health" || url.pathname === "/api/command") {
    return null;
  }

  const relativePath = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
  if (relativePath.includes("\0")) {
    throw new Error("静态资源路径包含非法空字符");
  }
  const filePath = path.resolve(clientRoot, relativePath);
  const relativeToClientRoot = path.relative(clientRoot, filePath);
  if (
    relativeToClientRoot === ".."
    || relativeToClientRoot.startsWith(`..${path.sep}`)
    || path.isAbsolute(relativeToClientRoot)
  ) {
    throw new Error(`静态资源路径越界: ${url.pathname}`);
  }
  return filePath;
}

async function readRequestJson(request) {
  const chunks = [];
  let size = 0;
  let oversized = false;
  for await (const chunk of request) {
    if (oversized) {
      continue;
    }
    size += chunk.length;
    if (size > MAX_COMMAND_BODY_BYTES) {
      chunks.length = 0;
      oversized = true;
      continue;
    }
    chunks.push(chunk);
  }
  if (oversized) {
    throw new HttpRequestError(413, "请求体过大");
  }
  const text = Buffer.concat(chunks).toString("utf8");
  if (!text.trim()) {
    throw new HttpRequestError(400, "请求体不能为空");
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new HttpRequestError(
      400,
      `请求体 JSON 无效: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

function writeJson(response, statusCode, payload) {
  response.writeHead(statusCode, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(payload));
}

async function handleCommandRequest(session, request, response) {
  if (request.method !== "POST") {
    writeJson(response, 405, { error: "只支持 POST /api/command" });
    return;
  }
  const body = await readRequestJson(request);
  if (!body || typeof body.line !== "string") {
    throw new HttpRequestError(400, "请求体必须包含字符串字段 line");
  }
  let browserControlCommand;
  try {
    browserControlCommand = parseBrowserControlCommand(body.line);
  } catch (error) {
    throw new HttpRequestError(
      400,
      error instanceof Error ? error.message : String(error),
    );
  }
  if (browserControlCommand.name === "exit") {
    throw new HttpRequestError(400, "exit 命令只允许在服务进程 stdin 中使用");
  }
  const commandResult = await executeBrowserCommand(session, browserControlCommand);
  const state = commandResult.state ?? await session.currentState();
  writeJson(response, 200, { ok: true, output: commandResult.output, state });
}

function forwardedHeaders(headers) {
  const forwarded = {};
  const hopByHopHeaders = new Set(HOP_BY_HOP_HEADERS);
  for (const headerName of ["connection", "proxy-connection"]) {
    for (const option of (headers.get(headerName) ?? "").split(",")) {
      const normalizedOption = option.trim().toLowerCase();
      if (normalizedOption) {
        hopByHopHeaders.add(normalizedOption);
      }
    }
  }
  for (const [key, value] of headers) {
    const normalizedName = key.toLowerCase();
    if (hopByHopHeaders.has(normalizedName) || DECODED_BODY_HEADERS.has(normalizedName)) {
      continue;
    }
    forwarded[key] = value;
  }
  return forwarded;
}

async function proxyCdpHttp(session, request, response) {
  if (request.method !== "GET") {
    writeJson(response, 405, { error: "CDP HTTP 代理只支持 GET" });
    return;
  }
  const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
  const upstreamUrl = new URL(`${requestUrl.pathname}${requestUrl.search}`, session.cdpHttpOrigin());
  const controller = new AbortController();
  const abortOnDisconnect = () => controller.abort();
  const abortIfResponseClosed = () => {
    if (!response.writableFinished) {
      controller.abort();
    }
  };
  request.once("aborted", abortOnDisconnect);
  response.once("close", abortIfResponseClosed);

  try {
    if (request.aborted || response.destroyed) {
      controller.abort();
      return;
    }
    const upstreamResponse = await fetch(upstreamUrl, {
      headers: {
        "accept-encoding": "identity",
      },
      signal: controller.signal,
    });
    response.writeHead(upstreamResponse.status, forwardedHeaders(upstreamResponse.headers));
    if (upstreamResponse.body) {
      await pipeline(Readable.fromWeb(upstreamResponse.body), response);
      return;
    }
    response.end();
  } catch (error) {
    if (!controller.signal.aborted) {
      throw error;
    }
  } finally {
    request.off("aborted", abortOnDisconnect);
    response.off("close", abortIfResponseClosed);
  }
}

async function redirectToDevTools(session, request, response) {
  const debugPageTarget = await session.currentDebugTarget();
  const publicHost = request.headers.host;
  if (!publicHost) {
    throw new Error("请求缺少 Host header，无法生成 DevTools WebSocket 地址");
  }
  const devToolsWebSocketTarget = `${publicHost}/cdp/page/${encodeURIComponent(debugPageTarget.id)}`;
  const devToolsInspectorUrl = `/devtools/inspector.html?ws=${encodeURIComponent(devToolsWebSocketTarget)}`;
  response.writeHead(302, {
    location: devToolsInspectorUrl,
    "cache-control": "no-store",
  });
  response.end();
}

async function handleHttpRequest(session, request, response) {
  try {
    const url = new URL(request.url || "/", "http://127.0.0.1");
    if (url.pathname === "/health") {
      const state = await session.currentState();
      writeJson(response, 200, { ok: true, browserId: FIXED_BROWSER_ID, state });
      return;
    }

    if (url.pathname === "/api/command") {
      await handleCommandRequest(session, request, response);
      return;
    }

    if (url.pathname === "/devtools" || url.pathname === "/devtools/page") {
      await redirectToDevTools(session, request, response);
      return;
    }

    if (url.pathname.startsWith("/devtools/") || url.pathname.startsWith("/json/")) {
      await proxyCdpHttp(session, request, response);
      return;
    }

    const filePath = resolveStaticPath(request.url);
    const content = await readFile(filePath);
    response.writeHead(200, { "content-type": mimeType(filePath) });
    response.end(content);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const statusCode = error instanceof HttpRequestError
      ? error.statusCode
      : error?.code === "ENOENT"
        ? 404
        : 500;
    console.error(`[http] ${request.url} ${statusCode}: ${message}`);
    if (response.destroyed) {
      return;
    }
    if (response.headersSent) {
      response.destroy();
      return;
    }
    if (request.url?.startsWith("/api/")) {
      writeJson(response, statusCode, { error: message });
      return;
    }
    response.writeHead(statusCode, { "content-type": "text/plain; charset=utf-8" });
    response.end(message);
  }
}

export function createHttpRequestHandler(session) {
  return (request, response) => {
    void handleHttpRequest(session, request, response);
  };
}
