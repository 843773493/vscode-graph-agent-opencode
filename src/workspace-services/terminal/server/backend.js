import http from "node:http";
import path from "node:path";
import { existsSync, statSync } from "node:fs";
import { WebSocket, WebSocketServer } from "ws";
import {
  encodeTerminalServerMessage,
  parseTerminalClientMessage,
} from "../protocol/messages.js";
import { TerminalManager, resolveWorkspaceRoot } from "./terminalManager.js";
import { validateWorkspaceId } from "./workspaceIdentity.js";

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = 8012;
const WEBSOCKET_HIGH_WATERMARK_BYTES = 512 * 1024;

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

function resolveRequiredWorkspaceId(args) {
  const configured = args.has("workspace-id")
    ? args.get("workspace-id")
    : process.env.BOXTEAM_WORKSPACE_ID;
  return validateWorkspaceId(configured);
}

class WorkspaceAddressError extends Error {
  constructor(message, statusCode) {
    super(message);
    this.statusCode = statusCode;
  }
}

function requestWorkspaceId(request, url, mountedWorkspaceId) {
  const header = request.headers["x-boxteam-workspace-id"];
  const queryValues = url.searchParams.getAll("workspace_id");
  if (queryValues.length > 1 || Array.isArray(header)) {
    throw new WorkspaceAddressError("workspace_id 必须是单一显式 UUID", 400);
  }
  const query = queryValues[0];
  if (header !== undefined && query !== undefined && header !== query) {
    throw new WorkspaceAddressError("workspace_id 头部与查询载体冲突", 400);
  }
  const selected = header ?? query;
  if (selected === undefined) {
    throw new WorkspaceAddressError("缺少显式 workspace_id", 400);
  }
  try {
    validateWorkspaceId(selected);
  } catch (error) {
    throw new WorkspaceAddressError(error.message, 400);
  }
  if (selected !== mountedWorkspaceId) {
    throw new WorkspaceAddressError(`未挂载的终端工作区 UUID: ${selected}`, 404);
  }
  return selected;
}

function requireResourceOwner(request) {
  const address = request.socket.remoteAddress;
  if (!new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]).has(address)
    || request.headers["x-boxteam-resource-owner"] !== "workspace-terminal"
    || request.headers.origin !== undefined
    || request.headers["sec-fetch-site"] !== undefined) {
    throw new WorkspaceAddressError("Terminal 管理仅接受工作区 owner 的 loopback 通道", 403);
  }
}

function wsClient(socket) {
  return {
    sendRaw(message) {
      if (socket.readyState !== WebSocket.OPEN) {
        return false;
      }
      if (socket.bufferedAmount > WEBSOCKET_HIGH_WATERMARK_BYTES) {
        return false;
      }
      socket.send(message);
      return true;
    },
    sendJson(message) {
      return this.sendRaw(encodeTerminalServerMessage(message));
    },
    close(code, reason) {
      socket.close(code, reason);
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
  const workspaceId = resolveRequiredWorkspaceId(args);
  const manager = new TerminalManager({
    workspaceRoot,
    workspaceId,
  });
  await manager.init();

  const server = http.createServer(async (request, response) => {
    try {
      const { url, pathname } = normalizePathname(request);

      if (request.method === "GET" && pathname === "/health") {
        sendJson(response, 200, {
          ok: true,
          process_id: process.pid,
          workspace_id: workspaceId,
          terminal_count: manager.terminalsById.size,
        });
        return;
      }

      if (pathname.startsWith("/api/terminals")) {
        requireResourceOwner(request);
        requestWorkspaceId(request, url, workspaceId);
      }

      if (request.method === "GET" && pathname === "/api/terminals") {
        sendJson(response, 200, {
          data: manager.list(),
        });
        return;
      }

      if (request.method === "POST" && pathname === "/api/terminals") {
        const payload = await readJson(request);
        for (const field of ["session_id", "source_thread_id", "source_thread", "retention_scope", "thread_associations"]) {
          if (Object.hasOwn(payload, field)) {
            throw new WorkspaceAddressError(`Terminal 传输层不接受领域归属字段: ${field}`, 400);
          }
        }
        if (payload.workspace_id !== workspaceId) {
          throw new WorkspaceAddressError("workspace_id 请求载体与终端 owner 冲突", 400);
        }
        const terminal = await manager.create({
          workspaceId: payload.workspace_id,
          terminalId: payload.terminal_id,
          ownerRevision: payload.owner_revision,
          agentId: payload.agent_id,
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
          sendJson(response, 200, { data: manager.get(terminalId).snapshot() });
          return;
        }

        if (request.method === "POST" && action === "owner-revision") {
          const payload = await readJson(request);
          if (typeof payload.for_stop !== "boolean") {
            const error = new Error("Terminal owner revision必须明确for_stop意图");
            error.statusCode = 400;
            throw error;
          }
          await manager.syncOwnerRevision(terminalId, payload.owner_revision);
          sendJson(response, 200, { data: manager.get(terminalId).snapshot() });
          return;
        }

        if (request.method === "POST" && action === "transport-grant") {
          const payload = await readJson(request);
          sendJson(response, 200, { data: manager.issueTransportGrant(terminalId, {
            ownerRevision: payload.owner_revision, association: payload.association,
          }) });
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

        if (request.method === "POST" && action === "read") {
          sendJson(response, 200, { data: await manager.read(terminalId) });
          return;
        }

        if (request.method === "POST" && action === "background") {
          sendJson(response, 200, {
            data: await manager.markModelBackgrounded(terminalId),
          });
          return;
        }

        if (request.method === "POST" && action === "claim-steering") {
          const payload = await readJson(request);
          sendJson(response, 200, { data: await manager.claimSteering(terminalId, {
            completionEventId: payload.completion_event_id,
            commandStartSequence: payload.command_start_sequence,
          }) });
          return;
        }

        if (request.method === "POST" && action === "finish-steering") {
          const payload = await readJson(request);
          if (typeof payload.dispatched !== "boolean") {
            throw new Error("dispatched 必须是布尔值");
          }
          sendJson(response, 200, {
            data: await manager.finishSteering(terminalId, {
              completionEventId: payload.completion_event_id,
              commandStartSequence: payload.command_start_sequence,
              dispatched: payload.dispatched,
            }),
          });
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
          const payload = await readJson(request);
          const reason = payload.reason || "terminal_cancel";
          if (!new Set(["terminal_cancel", "model_requested"]).has(reason)) {
            throw new Error(`不支持的终端终止原因: ${reason}`);
          }
          sendJson(response, 200, {
            data: await manager.kill(terminalId, {
              reason,
            }),
          });
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
      const status = error.statusCode ?? (message.startsWith("终端不存在") ? 404 : 500);
      sendJson(response, status, {
        error: message,
        ...(error.creationReceipt ? { creation_receipt: error.creationReceipt } : {}),
      });
    }
  });

  const wss = new WebSocketServer({ noServer: true });
  server.on("upgrade", (request, socket, head) => {
    try {
      const { url, pathname } = normalizePathname(request);
      if (pathname !== "/terminal") {
        throw new WorkspaceAddressError("未知终端 WebSocket 路径", 404);
      }
      requestWorkspaceId(request, url, workspaceId);
      wss.handleUpgrade(request, socket, head, (connection) => {
        wss.emit("connection", connection, request);
      });
    } catch (error) {
      const status = error.statusCode ?? 500;
      const body = error.message;
      socket.end(`HTTP/1.1 ${status} Workspace Address Rejected\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`);
    }
  });
  wss.on("connection", (socket) => {
    const client = wsClient(socket);
    let attachedTerminal = null;
    let messageQueue = Promise.resolve();

    const detachCurrent = async () => {
      const currentTerminal = attachedTerminal;
      attachedTerminal = null;
      if (currentTerminal) {
        currentTerminal.transports.delete(client);
        await currentTerminal.detach(client);
      }
    };

    const processMessage = async (raw) => {
      const message = parseTerminalClientMessage(raw);

      if (message.type === "attach") {
        await detachCurrent();
        const targetTerminal = manager.get(message.terminalId);
        const grant = manager.consumeTransportGrant(message.terminalId, message.grantToken);
        if (message.cols && message.rows) {
          await manager.resize(
            targetTerminal.id,
            parsePositiveInt(message.cols, "cols"),
            parsePositiveInt(message.rows, "rows"),
          );
        }
        if (grant.transportOwnerRevision !== targetTerminal.transportOwnerRevision) {
          throw new Error(`终端连接授权已过期: terminal_id=${targetTerminal.id}`);
        }
        await targetTerminal.attach(client, {
          afterSequence: message.afterSequence,
          grant,
        });
        attachedTerminal = targetTerminal;
        return;
      }

      if (message.type === "detach") {
        await detachCurrent();
        return;
      }

      const currentTerminal = attachedTerminal;
      if (!currentTerminal) {
        throw new Error("尚未 attach 到终端");
      }
      currentTerminal.assertTransport(client);

      if (message.type === "input" || message.type === "agentInput") {
        if (typeof message.data !== "string") {
          throw new Error("input.data 必须是字符串");
        }
        await manager.write(currentTerminal.id, message.data, {
          source: message.type === "agentInput" ? "agent" : "user",
        });
        return;
      }

      if (message.type === "resize") {
        await manager.resize(
          currentTerminal.id,
          parsePositiveInt(message.cols, "cols"),
          parsePositiveInt(message.rows, "rows"),
        );
        return;
      }

      if (message.type === "ack") {
        currentTerminal.acknowledge(client, message.sequence);
        return;
      }

      throw new Error(`未知 WebSocket 消息类型: ${message.type}`);
    };

    socket.on("message", (raw) => {
      messageQueue = messageQueue
        .then(() => processMessage(raw))
        .catch((error) => {
        client.sendJson({
          type: "error",
          message: error instanceof Error ? error.message : String(error),
        });
        });
    });

    socket.on("close", () => {
      messageQueue = messageQueue
        .then(() => detachCurrent())
        .catch((error) => {
          console.error("[terminal-backend] WebSocket 关闭时 detach 失败", error);
        });
    });
  });

  server.listen(port, host, () => {
    console.log(`[terminal-backend] listening on http://${host}:${port}`);
    console.log(`[terminal-backend] workspace ${workspaceRoot}`);
    console.log(`[terminal-backend] workspace_id ${workspaceId}`);
  });

  let shuttingDown = false;
  let hasFatalError = false;
  const shutdown = async (reason, exitCode, error = null) => {
    if (error) {
      hasFatalError = true;
      console.error(error);
    }
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    for (const client of wss.clients) {
      client.close(1001, "terminal manager shutting down");
    }
    let finalExitCode = exitCode;
    try {
      await manager.shutdown(reason);
    } catch (shutdownError) {
      console.error(hasFatalError ? "[terminal-backend] 致命错误后的关闭失败:"
        : "[terminal-backend] 关闭失败，保留原任务与 HTTP 管理入口；核实后再次发送关闭信号重试:", shutdownError);
      if (!hasFatalError) {
        shuttingDown = false;
        return;
      }
    }
    // TODO(5.4)：HTTP排空与同步阻塞的总期限、未知启动的原生进程接管仍需独立核实。
    try {
      wss.close();
      await closeHttpServer(server);
    } catch (transportError) {
      finalExitCode = 1;
      console.error("[terminal-backend] transport 关闭失败", transportError);
    } finally {
      process.exit(hasFatalError ? 1 : finalExitCode);
    }
  };

  process.on("SIGINT", () => void shutdown("terminal_manager_sigint", 130));
  process.on("SIGTERM", () => void shutdown("terminal_manager_sigterm", 143));
  process.on("SIGHUP", () => void shutdown("terminal_manager_sighup", 129));
  if (process.platform === "win32") {
    process.on("SIGBREAK", () => void shutdown("terminal_manager_sigbreak", 143));
  }
  process.once("uncaughtException", (error) => {
    void shutdown("terminal_manager_uncaught_exception", 1, error);
  });
  process.once("unhandledRejection", (reason) => {
    const error = reason instanceof Error ? reason : new Error(String(reason));
    void shutdown("terminal_manager_unhandled_rejection", 1, error);
  });
}

await main();
