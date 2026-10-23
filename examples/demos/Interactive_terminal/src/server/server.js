import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer, WebSocket } from "ws";
import { FIXED_TERMINAL_ID, TerminalManager } from "./terminalManager.js";
import { encodeServerMessage, parseClientMessage } from "./protocol.js";

const clientRoot = fileURLToPath(new URL("../client/", import.meta.url));
const packageRoot = fileURLToPath(new URL("../../", import.meta.url));
const MAX_TERMINAL_SOCKET_BUFFERED_BYTES = 8 * 1024 * 1024;
const TERMINAL_SOCKET_SHUTDOWN_GRACE_MS = 1000;
const encodedOutputEvents = new WeakMap();

function readOptionValue(name) {
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
  const rawPort = process.env.INTERACTIVE_TERMINAL_PORT || readOptionValue("--port") || "8120";
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`非法端口: ${rawPort}`);
  }
  return port;
}

function resolveTerminalCwd() {
  const rawCwd = process.env.INTERACTIVE_TERMINAL_CWD || readOptionValue("--terminal-cwd") || packageRoot;
  return path.resolve(packageRoot, rawCwd);
}

function mimeType(filePath) {
  const ext = path.extname(filePath);
  if (ext === ".html") return "text/html; charset=utf-8";
  if (ext === ".js") return "text/javascript; charset=utf-8";
  if (ext === ".css") return "text/css; charset=utf-8";
  if (ext === ".json") return "application/json; charset=utf-8";
  return "application/octet-stream";
}

function parseRequestTarget(requestTarget) {
  return new URL(requestTarget || "/", "http://127.0.0.1");
}

function resolveStaticPath(url) {
  if (url.pathname === "/vendor/xterm/xterm.js") {
    return path.resolve(packageRoot, "node_modules", "@xterm", "xterm", "lib", "xterm.js");
  }
  if (url.pathname === "/vendor/xterm/xterm.css") {
    return path.resolve(packageRoot, "node_modules", "@xterm", "xterm", "css", "xterm.css");
  }
  if (url.pathname === "/vendor/xterm/addon-fit.js") {
    return path.resolve(packageRoot, "node_modules", "@xterm", "addon-fit", "lib", "addon-fit.js");
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

async function handleHttpRequest(request, response) {
  let filePath;
  try {
    const url = parseRequestTarget(request.url);
    if (url.pathname === "/health") {
      response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({ ok: true, terminalId: FIXED_TERMINAL_ID }));
      return;
    }

    filePath = resolveStaticPath(url);
    const content = await readFile(filePath);
    response.writeHead(200, { "content-type": mimeType(filePath) });
    response.end(content);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const statusCode = error?.code === "ENOENT" ? 404 : 500;
    console.error(`[http] ${request.url} ${statusCode}: ${message}`);
    if (response.destroyed) {
      return;
    }
    if (response.headersSent) {
      response.destroy();
      return;
    }
    response.writeHead(statusCode, { "content-type": "text/plain; charset=utf-8" });
    response.end(message);
  }
}

function sendJson(socket, message, clientId) {
  if (socket.readyState !== WebSocket.OPEN) {
    return false;
  }
  const encodedMessage = encodeServerMessage(message);
  return sendEncodedJson(socket, encodedMessage, Buffer.byteLength(encodedMessage), clientId);
}

function sendTerminalOutput(socket, outputEvent, clientId) {
  if (socket.readyState !== WebSocket.OPEN) {
    return false;
  }
  let encodedOutput = encodedOutputEvents.get(outputEvent);
  if (!encodedOutput) {
    const message = encodeServerMessage({ type: "output", ...outputEvent });
    encodedOutput = {
      message,
      byteLength: Buffer.byteLength(message),
    };
    encodedOutputEvents.set(outputEvent, encodedOutput);
  }
  return sendEncodedJson(socket, encodedOutput.message, encodedOutput.byteLength, clientId);
}

function sendEncodedJson(socket, encodedMessage, encodedByteLength, clientId) {
  if (socket.readyState !== WebSocket.OPEN) {
    return false;
  }
  if (
    socket.bufferedAmount + encodedByteLength > MAX_TERMINAL_SOCKET_BUFFERED_BYTES
  ) {
    console.warn(`[ws] ${clientId} 输出接收过慢，关闭连接以限制缓冲增长`);
    socket.close(1013, "terminal output buffer full");
    return false;
  }
  socket.send(encodedMessage);
  return true;
}

function sendSocketError(socket, error, clientId) {
  const message = error instanceof Error ? error.message : String(error);
  sendJson(socket, { type: "error", message }, clientId);
}

function createSocketHandler(terminalManager) {
  let nextClientNumber = 0;

  return (socket) => {
    const clientId = `client:${++nextClientNumber}`;
    let attachedTerminalId = null;

    socket.on("message", (raw) => {
      try {
        const message = parseClientMessage(raw);
        if (message.type === "attach") {
          const attached = terminalManager.attach({
            terminalId: message.terminalId,
            clientId,
            cols: message.cols,
            rows: message.rows,
            onOutput: (outputEvent) => sendTerminalOutput(
              socket,
              outputEvent,
              clientId,
            ),
            onExit: ({ exitCode, signal }) => {
              sendJson(
                socket,
                { type: "exit", terminalId: message.terminalId, exitCode, signal },
                clientId,
              );
              if (socket.readyState === WebSocket.OPEN) {
                socket.close(1000, "terminal exited");
              }
            },
          });
          attachedTerminalId = message.terminalId;
          sendJson(socket, { type: "attached", ...attached }, clientId);
          return;
        }

        if (message.type === "detach") {
          terminalManager.detach(message.terminalId, clientId);
          attachedTerminalId = null;
          sendJson(socket, { type: "detached", terminalId: message.terminalId }, clientId);
          return;
        }

        if (message.type === "input" || message.type === "agentInput") {
          terminalManager.write(message.terminalId, message.data);
          return;
        }

        if (message.type === "resize") {
          terminalManager.resize(message.terminalId, message.cols, message.rows);
          return;
        }
      } catch (error) {
        sendSocketError(socket, error, clientId);
        if (socket.readyState === WebSocket.OPEN) {
          socket.close(1002, "invalid terminal message");
        }
      }
    });

    socket.on("close", () => {
      if (attachedTerminalId) {
        terminalManager.detach(attachedTerminalId, clientId);
        attachedTerminalId = null;
      }
    });

    socket.on("error", (error) => {
      console.error(`[ws] ${clientId} 连接错误: ${error instanceof Error ? error.message : String(error)}`);
    });
  };
}

const host = process.env.INTERACTIVE_TERMINAL_HOST || readOptionValue("--host") || "127.0.0.1";
const port = resolvePort();
const terminalCwd = resolveTerminalCwd();
const terminalManager = new TerminalManager({ cwd: terminalCwd });
const httpSockets = new Set();
const server = createServer((request, response) => {
  void handleHttpRequest(request, response);
});
server.on("connection", (socket) => {
  httpSockets.add(socket);
  socket.once("close", () => httpSockets.delete(socket));
});
const webSocketServer = new WebSocketServer({ noServer: true });
let shutdownStarted = false;

function shutdown(signal) {
  if (shutdownStarted) {
    return;
  }
  shutdownStarted = true;
  console.log(`[interactive-terminal] 收到 ${signal}，正在关闭终端服务`);
  const forceCloseWebSocketTimer = setTimeout(() => {
    for (const socket of webSocketServer.clients) {
      socket.terminate();
    }
  }, TERMINAL_SOCKET_SHUTDOWN_GRACE_MS);
  forceCloseWebSocketTimer.unref();
  webSocketServer.once("close", () => clearTimeout(forceCloseWebSocketTimer));
  const forceCloseHttpTimer = setTimeout(() => {
    for (const socket of httpSockets) {
      socket.destroy();
    }
  }, TERMINAL_SOCKET_SHUTDOWN_GRACE_MS);
  forceCloseHttpTimer.unref();
  server.once("close", () => clearTimeout(forceCloseHttpTimer));
  webSocketServer.close();
  if (server.listening) {
    server.close();
  } else {
    server.once("listening", () => server.close());
  }
  terminalManager.killAll();
  for (const socket of webSocketServer.clients) {
    if (socket.readyState === WebSocket.OPEN) {
      socket.close(1001, "terminal server shutting down");
    }
  }
}

webSocketServer.on("connection", createSocketHandler(terminalManager));

server.on("upgrade", (request, socket, head) => {
  httpSockets.delete(socket);
  const url = new URL(request.url || "/", `http://${host}`);
  if (url.pathname !== "/terminal") {
    socket.write("HTTP/1.1 404 Not Found\r\n\r\n");
    socket.destroy();
    return;
  }
  webSocketServer.handleUpgrade(request, socket, head, (ws) => {
    webSocketServer.emit("connection", ws, request);
  });
});

server.listen(port, host, () => {
  console.log(`[interactive-terminal] http://${host}:${port}`);
  console.log(`[interactive-terminal] terminalId=${FIXED_TERMINAL_ID}`);
  console.log(`[interactive-terminal] terminal cwd=${terminalCwd}`);
});

process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
