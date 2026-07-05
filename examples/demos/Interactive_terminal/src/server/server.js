import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer, WebSocket } from "ws";
import { FIXED_TERMINAL_ID, TerminalManager } from "./terminalManager.js";
import { encodeServerMessage, parseClientMessage } from "./protocol.js";

const clientRoot = fileURLToPath(new URL("../client/", import.meta.url));
const packageRoot = process.cwd();

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
  const rawPort = process.env.INTERACTIVE_TERMINAL_PORT || readArg("--port") || "8120";
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`非法端口: ${rawPort}`);
  }
  return port;
}

function resolveTerminalCwd() {
  const rawCwd = process.env.INTERACTIVE_TERMINAL_CWD || readArg("--terminal-cwd") || packageRoot;
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

function resolveStaticPath(requestUrl) {
  const url = new URL(requestUrl || "/", "http://127.0.0.1");
  if (url.pathname === "/health") {
    return null;
  }
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
  if (!filePath.startsWith(clientRoot)) {
    throw new Error(`静态资源路径越界: ${url.pathname}`);
  }
  return filePath;
}

async function handleHttpRequest(request, response) {
  if (request.url?.startsWith("/health")) {
    response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ ok: true, terminalId: FIXED_TERMINAL_ID }));
    return;
  }

  let filePath;
  try {
    filePath = resolveStaticPath(request.url);
    const content = await readFile(filePath);
    response.writeHead(200, { "content-type": mimeType(filePath) });
    response.end(content);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const statusCode = error?.code === "ENOENT" ? 404 : 500;
    console.error(`[http] ${request.url} ${statusCode}: ${message}`);
    response.writeHead(statusCode, { "content-type": "text/plain; charset=utf-8" });
    response.end(message);
  }
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
            onOutput: (data) => sendJson(socket, { type: "output", terminalId: message.terminalId, data }),
            onExit: ({ exitCode, signal }) => {
              sendJson(socket, { type: "exit", terminalId: message.terminalId, exitCode, signal });
            },
          });
          attachedTerminalId = message.terminalId;
          sendJson(socket, { type: "attached", ...attached });
          return;
        }

        if (message.type === "detach") {
          terminalManager.detach(message.terminalId, clientId);
          attachedTerminalId = null;
          sendJson(socket, { type: "detached", terminalId: message.terminalId });
          return;
        }

        if (message.type === "input") {
          terminalManager.write(message.terminalId, message.data);
          return;
        }

        if (message.type === "agentInput") {
          terminalManager.agentWrite(message.terminalId, message.data);
          return;
        }

        if (message.type === "resize") {
          terminalManager.resize(message.terminalId, message.cols, message.rows);
          return;
        }
      } catch (error) {
        sendSocketError(socket, error);
        socket.close(1002, "invalid terminal message");
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

const host = process.env.INTERACTIVE_TERMINAL_HOST || readArg("--host") || "127.0.0.1";
const port = resolvePort();
const terminalCwd = resolveTerminalCwd();
const terminalManager = new TerminalManager({ cwd: terminalCwd });
const server = createServer((request, response) => {
  void handleHttpRequest(request, response);
});
const webSocketServer = new WebSocketServer({ noServer: true });

webSocketServer.on("connection", createSocketHandler(terminalManager));

server.on("upgrade", (request, socket, head) => {
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
