import http from "node:http";
import path from "node:path";
import { existsSync, statSync } from "node:fs";
import { WebSocket, WebSocketServer } from "ws";
import { TerminalManager, resolveWorkspaceRoot } from "./terminalManager.js";

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = 8012;

function parseArgs(argv) {
  const args = new Map();
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) {
      continue;
    }
    const key = arg.slice(2);
    const value = argv[index + 1]?.startsWith("--") ? "true" : argv[index + 1];
    args.set(key, value ?? "true");
    if (value && value !== "true") {
      index += 1;
    }
  }
  return args;
}

function sendJson(response, status, data) {
  const body = JSON.stringify(data);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,POST,DELETE,OPTIONS",
    "access-control-allow-headers": "content-type",
  });
  response.end(body);
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw.trim()) {
    return {};
  }
  return JSON.parse(raw);
}

function notFound(response) {
  sendJson(response, 404, { error: "not_found" });
}

function missingTerminalSnapshot(manager, terminalId) {
  const timestamp = new Date().toISOString();
  return {
    terminal_id: terminalId,
    session_id: "",
    title: "Deleted Terminal",
    command: "",
    args: [],
    cwd: "",
    cols: 100,
    rows: 30,
    status: "deleted",
    created_at: timestamp,
    updated_at: timestamp,
    started_at: null,
    ended_at: timestamp,
    exit_code: null,
    signal: null,
    os_pid: null,
    sequence: 0,
    buffer: "",
    display_buffer: "",
    last_command: null,
    last_command_status: "deleted",
    last_command_exit_code: null,
    last_command_started_at: null,
    last_command_completed_at: timestamp,
    client_count: 0,
    attach_url: manager.attachUrl(terminalId),
  };
}

function normalizePathname(request) {
  const url = new URL(request.url || "/", "http://127.0.0.1");
  return { url, pathname: decodeURIComponent(url.pathname) };
}

function parsePositiveInt(value, fieldName) {
  const numberValue = Number(value);
  if (!Number.isInteger(numberValue) || numberValue <= 0) {
    throw new Error(`${fieldName} 必须是正整数`);
  }
  return numberValue;
}

function resolveRequiredWorkspaceRoot(args) {
  const raw = args.has("workspace-root")
    ? args.get("workspace-root")
    : resolveWorkspaceRoot();
  if (typeof raw !== "string" || raw.trim() === "" || raw === "true") {
    throw new Error("--workspace-root 必须提供有效路径值");
  }
  const resolved = path.resolve(raw);
  if (!existsSync(resolved) || !statSync(resolved).isDirectory()) {
    throw new Error(`--workspace-root 必须指向已存在的目录: ${resolved}`);
  }
  return resolved;
}

function wsClient(socket) {
  return {
    sendRaw(message) {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(message);
      }
    },
    sendJson(message) {
      this.sendRaw(JSON.stringify(message));
    },
  };
}

function closeHttpServer(server) {
  return new Promise((resolve, reject) => {
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
  const args = parseArgs(process.argv.slice(2));
  const host = args.get("host") || process.env.BOXTEAM_TERMINAL_HOST || DEFAULT_HOST;
  const port = Number(args.get("port") || process.env.BOXTEAM_TERMINAL_BACKEND_PORT || DEFAULT_PORT);
  const workspaceRoot = resolveRequiredWorkspaceRoot(args);
  const terminalFrontendBaseUrl =
    args.get("frontend-url") ||
    process.env.BOXTEAM_TERMINAL_FRONTEND_URL ||
    "http://127.0.0.1:8013";

  const manager = new TerminalManager({
    workspaceRoot,
    terminalFrontendBaseUrl,
  });
  await manager.init();

  const server = http.createServer(async (request, response) => {
    response.setHeader("access-control-allow-origin", "*");
    response.setHeader("access-control-allow-methods", "GET,POST,DELETE,OPTIONS");
    response.setHeader("access-control-allow-headers", "content-type");
    if (request.method === "OPTIONS") {
      response.writeHead(204);
      response.end();
      return;
    }

    try {
      const { url, pathname } = normalizePathname(request);

      if (request.method === "GET" && pathname === "/health") {
        sendJson(response, 200, {
          ok: true,
          workspace_root: workspaceRoot,
          terminal_count: manager.list().length,
        });
        return;
      }

      if (request.method === "GET" && pathname === "/api/terminals") {
        sendJson(response, 200, {
          data: manager.list({ sessionId: url.searchParams.get("session_id") }),
        });
        return;
      }

      if (request.method === "POST" && pathname === "/api/terminals") {
        const payload = await readJson(request);
        const terminal = await manager.create({
          sessionId: payload.session_id,
          title: payload.title,
          cwd: payload.cwd,
          cols: payload.cols,
          rows: payload.rows,
          command: payload.command,
          args: payload.args,
        });
        sendJson(response, 200, { data: terminal });
        return;
      }

      const terminalMatch = pathname.match(/^\/api\/terminals\/([^/]+)(?:\/([^/]+))?$/);
      if (terminalMatch) {
        const terminalId = terminalMatch[1];
        const action = terminalMatch[2] || "";

        if (request.method === "GET" && !action) {
          try {
            sendJson(response, 200, { data: manager.get(terminalId).snapshot() });
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            if (
              message.startsWith("终端不存在") &&
              url.searchParams.get("missing_as_deleted") === "1"
            ) {
              sendJson(response, 200, {
                data: missingTerminalSnapshot(manager, terminalId),
              });
              return;
            }
            throw error;
          }
          return;
        }

        if (request.method === "POST" && action === "write") {
          const payload = await readJson(request);
          if (typeof payload.data !== "string" || payload.data.length === 0) {
            throw new Error("data 不能为空");
          }
          const terminal = await manager.write(terminalId, payload.data, {
            source: payload.source || "agent",
            command: typeof payload.command === "string" ? payload.command : null,
          });
          sendJson(response, 200, { data: terminal });
          return;
        }

        if (request.method === "POST" && action === "resize") {
          const payload = await readJson(request);
          const terminal = await manager.resize(
            terminalId,
            parsePositiveInt(payload.cols, "cols"),
            parsePositiveInt(payload.rows, "rows"),
          );
          sendJson(response, 200, { data: terminal });
          return;
        }

        if (request.method === "POST" && action === "kill") {
          sendJson(response, 200, { data: await manager.kill(terminalId) });
          return;
        }

        if (request.method === "DELETE" && !action) {
          sendJson(response, 200, { data: await manager.delete(terminalId) });
          return;
        }
      }

      notFound(response);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const status = message.startsWith("终端不存在") ? 404 : 500;
      sendJson(response, status, {
        error: message,
      });
    }
  });

  const wss = new WebSocketServer({ server, path: "/terminal" });
  wss.on("connection", (socket) => {
    const client = wsClient(socket);
    let attachedTerminal = null;

    const detachCurrent = async () => {
      if (attachedTerminal) {
        await attachedTerminal.detach(client);
        attachedTerminal = null;
      }
    };

    socket.on("message", async (raw) => {
      try {
        const message = JSON.parse(raw.toString("utf8"));
        if (!message || typeof message !== "object") {
          throw new Error("WebSocket 消息必须是 JSON object");
        }

        if (message.type === "attach") {
          await detachCurrent();
          attachedTerminal = manager.get(message.terminalId);
          if (message.cols && message.rows) {
            await manager.resize(
              message.terminalId,
              parsePositiveInt(message.cols, "cols"),
              parsePositiveInt(message.rows, "rows"),
            );
          }
          await attachedTerminal.attach(client);
          return;
        }

        if (message.type === "detach") {
          await detachCurrent();
          return;
        }

        if (!attachedTerminal) {
          throw new Error("尚未 attach 到终端");
        }

        if (message.type === "input" || message.type === "agentInput") {
          if (typeof message.data !== "string") {
            throw new Error("input.data 必须是字符串");
          }
          await manager.write(attachedTerminal.id, message.data, {
            source: message.type === "agentInput" ? "agent" : "user",
          });
          return;
        }

        if (message.type === "resize") {
          await manager.resize(
            attachedTerminal.id,
            parsePositiveInt(message.cols, "cols"),
            parsePositiveInt(message.rows, "rows"),
          );
          return;
        }

        throw new Error(`未知 WebSocket 消息类型: ${message.type}`);
      } catch (error) {
        client.sendJson({
          type: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      }
    });

    socket.on("close", () => {
      void detachCurrent();
    });
  });

  server.listen(port, host, () => {
    console.log(`[terminal-backend] listening on http://${host}:${port}`);
    console.log(`[terminal-backend] workspace ${workspaceRoot}`);
  });

  let shuttingDown = false;
  const shutdown = async (reason, exitCode, error = null) => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    if (error) {
      console.error(error);
    }
    for (const client of wss.clients) {
      client.close(1001, "terminal manager shutting down");
    }
    await manager.shutdown(reason);
    wss.close();
    await closeHttpServer(server);
    process.exit(exitCode);
  };

  process.once("SIGINT", () => void shutdown("terminal_manager_sigint", 130));
  process.once("SIGTERM", () => void shutdown("terminal_manager_sigterm", 143));
  process.once("SIGHUP", () => void shutdown("terminal_manager_sighup", 129));
  process.once("uncaughtException", (error) => {
    void shutdown("terminal_manager_uncaught_exception", 1, error);
  });
  process.once("unhandledRejection", (reason) => {
    const error = reason instanceof Error ? reason : new Error(String(reason));
    void shutdown("terminal_manager_unhandled_rejection", 1, error);
  });
}

await main();
