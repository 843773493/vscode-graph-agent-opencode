import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { WebSocket, WebSocketServer } from "ws";
import { BrowserSession } from "./browserSession.js";
import { FIXED_BROWSER_ID, encodeServerMessage, parseClientMessage } from "./protocol.js";
import { executeBrowserCommand, parseTerminalCommand, TERMINAL_COMMAND_HELP } from "./terminalCommands.js";
import { normalizeHttpUrl } from "./url.js";

const clientRoot = fileURLToPath(new URL("../client/", import.meta.url));
const projectRoot = fileURLToPath(new URL("../../", import.meta.url));

function readArg(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return null;
  }
  const value = process.argv[index + 1];
  if (!value) {
    throw new Error(`缺少命令行参数 ${name} 的值`);
  }
  return value;
}

function resolvePort() {
  const rawPort = process.env.REMOTE_SCREEN_PORT || readArg("--port") || "8121";
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`非法端口: ${rawPort}`);
  }
  return port;
}

function resolveHeadless() {
  const raw = process.env.REMOTE_SCREEN_HEADLESS || readArg("--headless");
  if (process.argv.includes("--headed")) {
    return false;
  }
  if (raw === null) {
    return true;
  }
  if (raw === "1" || raw === "true") {
    return true;
  }
  if (raw === "0" || raw === "false") {
    return false;
  }
  throw new Error(`REMOTE_SCREEN_HEADLESS/--headless 必须是 true/false: ${raw}`);
}

function resolveViewport() {
  const rawViewport = process.env.REMOTE_SCREEN_VIEWPORT || readArg("--viewport") || "1920x800";
  const match = rawViewport.match(/^(\d+)x(\d+)$/i);
  if (!match) {
    throw new Error(`viewport 格式必须是 <width>x<height>: ${rawViewport}`);
  }
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new Error(`非法 viewport: ${rawViewport}`);
  }
  return { width, height };
}

function resolveCdpPort() {
  const rawPort = process.env.REMOTE_SCREEN_CDP_PORT || readArg("--cdp-port") || "9333";
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`非法 CDP 端口: ${rawPort}`);
  }
  return port;
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
  if (!filePath.startsWith(clientRoot)) {
    throw new Error(`静态资源路径越界: ${url.pathname}`);
  }
  return filePath;
}

async function readRequestJson(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 65536) {
      throw new Error("请求体过大");
    }
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  if (!text.trim()) {
    throw new Error("请求体不能为空");
  }
  return JSON.parse(text);
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
    throw new Error("请求体必须包含字符串字段 line");
  }
  const command = parseTerminalCommand(body.line);
  if (command.name === "exit") {
    throw new Error("exit 命令只允许在服务进程 stdin 中使用");
  }
  const output = await executeBrowserCommand(session, command);
  const state = await session.currentState();
  writeJson(response, 200, { ok: true, output, state });
}

function forwardedHeaders(headers) {
  const forwarded = {};
  for (const [key, value] of headers) {
    if (key === "connection" || key === "content-encoding" || key === "content-length" || key === "transfer-encoding") {
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
  const upstreamResponse = await fetch(upstreamUrl, {
    headers: {
      "accept-encoding": "identity",
    },
  });
  const body = Buffer.from(await upstreamResponse.arrayBuffer());
  response.writeHead(upstreamResponse.status, forwardedHeaders(upstreamResponse.headers));
  response.end(body);
}

async function redirectToDevTools(session, request, response) {
  const target = await session.currentDebugTarget();
  const publicHost = request.headers.host;
  if (!publicHost) {
    throw new Error("请求缺少 Host header，无法生成 DevTools WebSocket 地址");
  }
  const wsTarget = `${publicHost}/cdp/page/${encodeURIComponent(target.id)}`;
  const devtoolsUrl = `/devtools/inspector.html?ws=${encodeURIComponent(wsTarget)}`;
  response.writeHead(302, {
    location: devtoolsUrl,
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
    const statusCode = error?.code === "ENOENT" ? 404 : 500;
    console.error(`[http] ${request.url} ${statusCode}: ${message}`);
    if (request.url?.startsWith("/api/")) {
      writeJson(response, statusCode, { error: message });
      return;
    }
    response.writeHead(statusCode, { "content-type": "text/plain; charset=utf-8" });
    response.end(message);
  }
}

function proxyCdpWebSocket(session, targetId, clientSocket) {
  const upstreamSocket = new WebSocket(`${session.cdpWebSocketOrigin()}/devtools/page/${targetId}`);
  const pendingMessages = [];
  let upstreamOpen = false;

  function closeSocket(socket, code, reason) {
    if (socket.readyState !== WebSocket.OPEN) {
      return;
    }
    const safeCode = Number.isInteger(code) && code >= 1000 && code <= 4999 && code !== 1005 && code !== 1006 && code !== 1015
      ? code
      : 1000;
    socket.close(safeCode, reason);
  }

  upstreamSocket.on("open", () => {
    upstreamOpen = true;
    for (const message of pendingMessages) {
      upstreamSocket.send(message.payload, { binary: message.isBinary });
    }
    pendingMessages.length = 0;
  });

  clientSocket.on("message", (raw, isBinary) => {
    const payload = isBinary ? raw : raw.toString("utf8");
    if (upstreamOpen) {
      upstreamSocket.send(payload, { binary: isBinary });
      return;
    }
    pendingMessages.push({ payload, isBinary });
  });

  upstreamSocket.on("message", (raw, isBinary) => {
    if (clientSocket.readyState === WebSocket.OPEN) {
      const payload = isBinary ? raw : raw.toString("utf8");
      clientSocket.send(payload, { binary: isBinary });
    }
  });

  upstreamSocket.on("close", (code, reason) => {
    closeSocket(clientSocket, code, reason);
  });

  clientSocket.on("close", () => {
    if (upstreamSocket.readyState === WebSocket.OPEN) {
      closeSocket(upstreamSocket, 1000, "client closed");
      return;
    }
    if (upstreamSocket.readyState === WebSocket.CONNECTING) {
      upstreamSocket.terminate();
    }
  });

  upstreamSocket.on("error", (error) => {
    console.error(`[cdp-ws] 上游连接错误: ${error instanceof Error ? error.message : String(error)}`);
    if (clientSocket.readyState === WebSocket.OPEN) {
      clientSocket.close(1011, "cdp upstream error");
    }
  });

  clientSocket.on("error", (error) => {
    console.error(`[cdp-ws] DevTools 客户端连接错误: ${error instanceof Error ? error.message : String(error)}`);
  });
}

function sendJson(socket, message) {
  if (socket.readyState !== WebSocket.OPEN) {
    console.warn(`[ws] socket 未打开，跳过发送 ${message.type}`);
    return;
  }
  socket.send(encodeServerMessage(message));
}

function sendSocketError(socket, error) {
  const message = error instanceof Error ? error.message : String(error);
  sendJson(socket, { type: "error", message });
}

function assertAttached(attached) {
  if (!attached) {
    throw new Error("请先 attach 到 uuid:12");
  }
}

function commandFromClientMessage(message) {
  if (message.name === "goto") {
    return { name: "goto", url: message.url };
  }
  return { name: message.name };
}

function createSocketHandler(session) {
  let nextClientNumber = 0;

  return (socket) => {
    const clientId = `viewer:${++nextClientNumber}`;
    let attached = false;
    let queue = Promise.resolve();

    const onFrame = (frame) => {
      if (attached) {
        sendJson(socket, { type: "frame", ...frame });
      }
    };
    const onState = (state) => {
      if (attached) {
        sendJson(socket, { type: "state", browserId: session.browserId, state });
      }
    };

    async function detach(sendDetached) {
      if (!attached) {
        return;
      }
      attached = false;
      session.off("frame", onFrame);
      session.off("state", onState);
      const state = await session.detachClient(clientId);
      if (sendDetached) {
        sendJson(socket, { type: "detached", browserId: session.browserId, state });
      }
    }

    async function handleMessage(raw) {
      const message = parseClientMessage(raw);
      if (message.type === "attach") {
        if (attached) {
          throw new Error(`${clientId} 已经 attach`);
        }
        attached = true;
        session.on("frame", onFrame);
        session.on("state", onState);
        const state = await session.attachClient(clientId);
        sendJson(socket, { type: "attached", browserId: message.browserId, state });
        return;
      }

      if (message.type === "detach") {
        await detach(true);
        return;
      }

      assertAttached(attached);
      if (message.type === "pointer") {
        await session.dispatchPointer(message);
        return;
      }
      if (message.type === "key") {
        await session.dispatchKey(message);
        return;
      }
      if (message.type === "paste") {
        await session.insertText(message.text);
        return;
      }
      if (message.type === "viewport") {
        await session.setViewport(message.width, message.height);
        return;
      }
      if (message.type === "command") {
        const output = await executeBrowserCommand(session, commandFromClientMessage(message));
        const state = await session.currentState();
        sendJson(socket, { type: "commandResult", browserId: message.browserId, output, state });
        return;
      }
      throw new Error(`未处理消息类型: ${message.type}`);
    }

    socket.on("message", (raw) => {
      queue = queue
        .then(() => handleMessage(raw))
        .catch((error) => {
          sendSocketError(socket, error);
        });
    });

    socket.on("close", () => {
      void detach(false);
    });

    socket.on("error", (error) => {
      console.error(`[ws] ${clientId} 连接错误: ${error instanceof Error ? error.message : String(error)}`);
    });
  };
}

function startTerminalCommandLoop(session, shutdown) {
  if (!process.stdin.isTTY) {
    console.log("[remote-screen] stdin 不是 TTY，跳过终端命令循环");
    return;
  }

  console.log("[remote-screen] 终端命令可用:");
  for (const command of TERMINAL_COMMAND_HELP) {
    console.log(`  ${command}`);
  }

  const readline = createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: true,
    prompt: "browser> ",
  });
  let closed = false;
  let queue = Promise.resolve();

  readline.on("line", (line) => {
    queue = queue
      .then(async () => {
        const command = parseTerminalCommand(line);
        if (command.name === "exit") {
          closed = true;
          readline.close();
          await shutdown();
          return;
        }
        const output = await executeBrowserCommand(session, command);
        if (output) {
          console.log(output);
        }
      })
      .catch((error) => {
        console.error(`[terminal] ${error instanceof Error ? error.message : String(error)}`);
      })
      .finally(() => {
        if (!closed) {
          readline.prompt();
        }
      });
  });

  readline.on("close", () => {
    closed = true;
  });
  readline.prompt();
}

async function closeServer(server) {
  if (!server.listening) {
    return;
  }
  await new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
}

async function main() {
  const host = process.env.REMOTE_SCREEN_HOST || readArg("--host") || "0.0.0.0";
  const port = resolvePort();
  const initialUrl = normalizeHttpUrl(process.env.REMOTE_SCREEN_INITIAL_URL || readArg("--url") || "https://www.bilibili.com/");
  const session = new BrowserSession({
    browserId: FIXED_BROWSER_ID,
    initialUrl,
    headless: resolveHeadless(),
    viewport: resolveViewport(),
    cdpHost: "127.0.0.1",
    cdpPort: resolveCdpPort(),
  });
  await session.start();

  const server = createServer((request, response) => {
    void handleHttpRequest(session, request, response);
  });
  const webSocketServer = new WebSocketServer({ noServer: true });
  webSocketServer.on("connection", createSocketHandler(session));

  server.on("upgrade", (request, socket, head) => {
    const url = new URL(request.url || "/", `http://${host}`);
    if (url.pathname !== "/remote-screen") {
      if (url.pathname.startsWith("/cdp/page/")) {
        const targetId = decodeURIComponent(url.pathname.slice("/cdp/page/".length));
        webSocketServer.handleUpgrade(request, socket, head, (ws) => {
          proxyCdpWebSocket(session, targetId, ws);
        });
        return;
      }
      socket.write("HTTP/1.1 404 Not Found\r\n\r\n");
      socket.destroy();
      return;
    }
    webSocketServer.handleUpgrade(request, socket, head, (ws) => {
      webSocketServer.emit("connection", ws, request);
    });
  });

  let shuttingDown = false;
  async function shutdown() {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    console.log("[remote-screen] 正在关闭...");
    webSocketServer.close();
    await closeServer(server);
    await session.close();
    console.log("[remote-screen] 已关闭");
    process.exit(0);
  }

  process.on("SIGINT", () => {
    void shutdown();
  });
  process.on("SIGTERM", () => {
    void shutdown();
  });

  server.listen(port, host, () => {
    console.log(`[remote-screen] http://${host}:${port}`);
    console.log(`[remote-screen] browserId=${FIXED_BROWSER_ID}`);
    console.log(`[remote-screen] projectRoot=${projectRoot}`);
    console.log(`[remote-screen] initialUrl=${initialUrl}`);
  });
  startTerminalCommandLoop(session, shutdown);
}

main().catch((error) => {
  console.error(`[remote-screen] 启动失败: ${error instanceof Error ? error.stack : String(error)}`);
  process.exit(1);
});
