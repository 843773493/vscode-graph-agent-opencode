import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { createInterface } from "node:readline";
import { setTimeout as delay } from "node:timers/promises";
import { WebSocket, WebSocketServer } from "ws";

import { closeHttpServer } from "../../../shared/serverHttpShutdown.mjs";
import { readProcessStat, terminateProcessTree } from "../../../shared/serverProcessLifecycle.mjs";
import { createJsonRequestReader } from "../../../shared/serverJsonRequest.mjs";
import { createPluginRuntimeManagement } from "../../../shared/serverPluginRuntime.mjs";
import { createRuntimeLogger } from "../../../shared/serverRuntimeLogging.mjs";
import { verifyRuntimeControlToken } from "../../../shared/serverRuntimeControl.mjs";

const logger = createRuntimeLogger("debug", "debug.backend");
const INSPECTOR_METHODS = new Set([
  "Debugger.enable",
  "Debugger.evaluateOnCallFrame",
  "Debugger.pause",
  "Debugger.removeBreakpoint",
  "Debugger.resume",
  "Debugger.setBreakpointByUrl",
  "Debugger.stepInto",
  "Debugger.stepOut",
  "Debugger.stepOver",
  "NodeRuntime.notifyWhenWaitingForDisconnect",
  "Runtime.enable",
  "Runtime.getProperties",
  "Runtime.runIfWaitingForDebugger",
]);
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const MAX_FRAME_BYTES = 1_000_000;

function fail(message, statusCode = 400) {
  throw Object.assign(new Error(message), { statusCode });
}

function sendJson(socket, payload) {
  if (socket === null || socket.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(payload));
  return true;
}

function parseRequest(raw) {
  if (raw.length > MAX_FRAME_BYTES) fail("Debug 控制帧超过大小限制");
  let request;
  try {
    request = JSON.parse(raw.toString("utf8"));
  } catch (cause) {
    throw new TypeError("Debug 控制帧必须是合法 JSON", { cause });
  }
  if (request === null || typeof request !== "object" || Array.isArray(request)
      || request.version !== 1 || request.type !== "request"
      || typeof request.request_id !== "string" || !REQUEST_ID_PATTERN.test(request.request_id)
      || typeof request.operation_id !== "string" || !REQUEST_ID_PATTERN.test(request.operation_id)
      || typeof request.run_id !== "string" || !RUN_ID_PATTERN.test(request.run_id)) {
    fail("Debug 控制帧不符合 version 1 请求合同");
  }
  if (!["launch", "inspector.send", "stop", "status"].includes(request.operation)) {
    fail(`不支持的 Debug 控制操作: ${String(request.operation)}`);
  }
  if (request.payload === null || typeof request.payload !== "object" || Array.isArray(request.payload)) {
    fail("Debug 控制 payload 必须是对象");
  }
  return request;
}

function sendResponse(socket, request, result) {
  sendJson(socket, {
    version: 1,
    type: "response",
    request_id: request.request_id,
    operation_id: request.operation_id,
    run_id: request.run_id,
    ok: true,
    result,
  });
}

function sendError(socket, request, error) {
  sendJson(socket, {
    version: 1,
    type: "response",
    request_id: request.request_id,
    operation_id: request.operation_id,
    run_id: request.run_id,
    ok: false,
    error: error instanceof Error ? error.message : String(error),
  });
}

function emit(run, event, payload) {
  sendJson(run.socket, {
    version: 1,
    type: "event",
    request_id: run.requestId,
    run_id: run.runId,
    event,
    payload,
  });
}

function assertWorkspaceHeader(request, workspaceId) {
  const value = request.headers["x-boxteam-workspace-id"];
  if (typeof value !== "string" || value !== workspaceId) {
    fail("Debug WebSocket 缺少匹配的显式 workspace_id", 403);
  }
}

function normalizePathname(request) {
  const url = new URL(request.url || "/", "http://127.0.0.1");
  return decodeURIComponent(url.pathname);
}

async function readInspectorReady(child, run) {
  const stderr = createInterface({ input: child.stderr, crlfDelay: Infinity });
  const ready = new Promise((resolve, reject) => {
    let settled = false;
    stderr.on("line", (line) => {
      emit(run, "stderr", { text: line });
      if (settled) return;
      const match = line.match(/^Debugger listening on (ws:\/\/\S+)$/);
      if (match) {
        settled = true;
        resolve(match[1]);
        return;
      }
      if (/^Starting inspector on \S+ failed:/.test(line)) {
        settled = true;
        reject(new Error(line));
      }
    });
    child.once("error", (error) => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    });
    child.once("exit", (code, signal) => {
      if (!settled) {
        settled = true;
        reject(new Error(`Node 调试进程在 Inspector 就绪前退出: code=${code}, signal=${signal}`));
      }
    });
  });
  const timeout = new AbortController();
  try {
    const inspectorUrl = await Promise.race([
      ready,
      delay(10_000, undefined, { signal: timeout.signal })
        .then(() => { throw new Error("等待 Node Inspector 就绪超时（10 秒）"); }),
    ]);
    return { inspectorUrl, stderr };
  } finally {
    timeout.abort();
  }
}

function assertLaunchPayload(payload) {
  const { node_executable: executable, script_path: scriptPath, args, working_directory: cwd, process_instance_id: processInstanceId } = payload;
  const { inspector_host: inspectorHost, inspector_port: inspectorPort } = payload;
  if (typeof executable !== "string" || executable.length === 0
      || typeof scriptPath !== "string" || scriptPath.length === 0
      || typeof cwd !== "string" || cwd.length === 0
      || typeof processInstanceId !== "string" || !RUN_ID_PATTERN.test(processInstanceId)
      || !Array.isArray(args) || args.some((arg) => typeof arg !== "string")) {
    fail("Node Debug launch 缺少有效的目标 Node、script、cwd 或 args");
  }
  if (!["127.0.0.1", "localhost", "::1", "[::1]"].includes(inspectorHost)
      || !Number.isInteger(inspectorPort) || inspectorPort < 0 || inspectorPort > 65535) {
    fail("Node Inspector 必须使用目标机器的 loopback 地址及 0–65535 端口");
  }
  const inspectorAddress = inspectorHost.includes(":") ? `[${inspectorHost.replaceAll(/[\[\]]/g, "")}]` : inspectorHost;
  return { executable, scriptPath, cwd, args, processInstanceId, inspectorAddress, inspectorPort };
}

async function stopRun(run) {
  if (run.stopPromise) return run.stopPromise;
  const child = run.child;
  run.stopPromise = (async () => {
    if (!Number.isInteger(run.pid) || run.pid <= 0) {
      run.exitCode = child?.exitCode ?? run.exitCode;
      run.status = "exited";
      return run.exitCode;
    }
    const result = await terminateProcessTree({
      pid: run.pid,
      processSessionId: run.processStat?.processSessionId ?? null,
      processStartTime: run.processStat?.processStartTime ?? null,
    });
    if (result === "still_running") {
      throw new Error(`等待 Node 调试进程树退出超时: pid=${run.pid}`);
    }
    if (child && child.exitCode === null && child.signalCode === null) {
      await new Promise((resolve, reject) => {
        child.once("exit", resolve);
        child.once("error", reject);
      });
    }
    run.exitCode = child?.exitCode ?? run.exitCode;
    run.status = "exited";
    return run.exitCode;
  })();
  try {
    return await run.stopPromise;
  } catch (error) {
    // 失败只结束本次停止；后续用户操作仍须能核实并重试原目标。
    run.stopPromise = null;
    throw error;
  }
}

async function main() {
  const management = createPluginRuntimeManagement({
    service: "debug",
    logger,
    readJson: createJsonRequestReader(logger),
    shutdown: () => shutdown("debug_plugin_control", 0),
  });
  const { port, token, identity } = management;
  if (identity.instance_scope !== "workspace" || identity.workspace_id === null
      || !identity.services.some((item) => item.service_type === "debug"
        && item.protocol === "dap" && item.service_id === "dap")) {
    throw new Error("Debug 插件必须是 workspace 范围且导出 dap 服务");
  }
  const workspaceId = identity.workspace_id;
  // TODO：终态小记录仍保留到插件关闭；后续回收须由已持久结清的原 claim 确认。
  const runs = new Map();
  const server = createServer(async (request, response) => {
    try {
      const pathname = normalizePathname(request);
      if (await management.handleRequest(request, response, pathname)) return;
      response.writeHead(404);
      response.end();
    } catch (error) {
      response.writeHead(error.statusCode ?? 500, { "content-type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({ error: error.message }));
    }
  });
  const wss = new WebSocketServer({ noServer: true, maxPayload: MAX_FRAME_BYTES });
  server.on("upgrade", (request, socket, head) => {
    try {
      if (normalizePathname(request) !== "/debug-runtime") fail("未知 Debug WebSocket 路径", 404);
      verifyRuntimeControlToken(request.headers["x-local-token"], token);
      assertWorkspaceHeader(request, workspaceId);
      wss.handleUpgrade(request, socket, head, (connection) => wss.emit("connection", connection, request));
    } catch (error) {
      const status = error.statusCode ?? 500;
      const body = error.message;
      socket.end(`HTTP/1.1 ${status} Debug Runtime Rejected\r\nContent-Type: text/plain\r\nContent-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`);
    }
  });
  wss.on("connection", (socket) => {
    let queue = Promise.resolve();
    socket.on("message", (raw) => {
      queue = queue.then(async () => {
        const request = parseRequest(raw);
        try {
          let result;
          if (request.operation === "launch") {
            if (runs.has(request.run_id)) fail(`Debug run_id 已存在: ${request.run_id}`, 409);
            const launch = assertLaunchPayload(request.payload);
            const run = {
              runId: request.run_id,
              requestId: request.request_id,
              processInstanceId: launch.processInstanceId,
              socket,
              status: "starting",
              child: spawn(launch.executable === "node" ? process.execPath : launch.executable, [`--inspect-brk=${launch.inspectorAddress}:${launch.inspectorPort}`, launch.scriptPath, ...launch.args], {
                cwd: launch.cwd,
                stdio: ["ignore", "pipe", "pipe"],
                windowsHide: true,
                ...(process.platform === "win32" ? {} : { detached: true }),
              }),
              processStat: null,
              stopPromise: null,
              inspector: null,
              inspectorSocket: null,
              inspectorState: null,
              exitCode: null,
            };
            run.pid = run.child.pid ?? null;
            runs.set(request.run_id, run);
            run.child.stdout.on("data", (buffer) => emit(run, "stdout", { text: buffer.toString("utf8") }));
            run.child.once("exit", async (code, signal) => {
              run.exitCode = code;
              run.status = "stopping";
              void run.inspectorSocket?.close();
              // 根进程退出后仍可能留下后代；同一 owner 核实进程树后才发布终态。
              await stopRun(run);
              emit(run, "process.exited", { exit_code: code, signal, pid: run.pid });
            });
            run.child.once("close", () => {
              // 终态查询只需身份及结果；stdio 已排空后释放进程、Inspector 和暂停快照。
              run.child = null;
              run.inspectorState = null;
              run.inspectorSocket?.close();
              run.inspectorSocket = null;
            });
            try {
              const [{ inspectorUrl }, processStat] = await Promise.all([
                readInspectorReady(run.child, run),
                readProcessStat(run.pid),
              ]);
              run.processStat = processStat;
              run.inspectorUrl = inspectorUrl;
              run.inspectorSocket = new WebSocket(inspectorUrl, { handshakeTimeout: 10_000 });
              await new Promise((resolve, reject) => {
                run.inspectorSocket.once("open", resolve);
                run.inspectorSocket.once("error", reject);
              });
              run.inspectorSocket.on("message", (message) => {
                let inspectorMessage;
                try {
                  inspectorMessage = JSON.parse(message.toString("utf8"));
                } catch (cause) {
                  emit(run, "inspector.message", { invalid_json: true, detail: String(cause) });
                  return;
                }
                if (inspectorMessage.method === "Debugger.paused") {
                  run.inspectorState = { method: inspectorMessage.method, params: inspectorMessage.params };
                } else if (inspectorMessage.method === "Debugger.resumed") {
                  run.inspectorState = null;
                }
                emit(run, "inspector.message", inspectorMessage);
                if (inspectorMessage.method === "NodeRuntime.waitingForDisconnect") {
                  run.inspectorSocket?.close();
                }
              });
              run.inspectorSocket.on("close", () => {
                if (run.status !== "exited") emit(run, "inspector.disconnected", {});
              });
              run.status = "running";
              result = { run_id: run.runId, pid: run.pid,
                process_instance_id: launch.processInstanceId, status: run.status };
            } catch (error) {
              await stopRun(run);
              // 保留同一 run 的终态，供 Backend 结清 claim 及丢回执后的只读查询。
              throw error;
            }
          } else {
            const run = runs.get(request.run_id);
            if (!run) fail(`Debug run 不存在: ${request.run_id}`, 404);
            run.socket = socket;
            run.requestId = request.request_id;
            if (request.operation === "status") {
              if (run.stopPromise) await run.stopPromise;
              result = { run_id: run.runId, pid: run.pid, status: run.status,
                exit_code: run.exitCode, process_instance_id: run.processInstanceId ?? null,
                inspector_url: run.inspectorUrl ?? null,
                inspector_state: run.inspectorState };
            } else if (request.operation === "stop") {
              result = { run_id: run.runId, exit_code: await stopRun(run), status: run.status };
              run.inspectorSocket?.close();
            } else {
              const { command_id: commandId, method, params } = request.payload;
              if (!Number.isSafeInteger(commandId) || commandId < 1 || !INSPECTOR_METHODS.has(method)) {
                fail("Inspector 命令不在允许的 NodeDebugInspector 方法集合中");
              }
              if (run.status === "exited" || run.inspectorSocket?.readyState !== WebSocket.OPEN) {
                fail("Node Inspector 连接不可用", 409);
              }
              run.inspectorSocket.send(JSON.stringify({ id: commandId, method, ...(params ? { params } : {}) }));
              result = { accepted: true, command_id: commandId };
            }
          }
          sendResponse(socket, request, result);
        } catch (error) {
          sendError(socket, request, error);
        }
      }).catch((error) => {
        logger.error("Debug 控制消息处理失败", error);
        socket.close(1008, "invalid_debug_runtime_request");
      });
    });
    socket.on("close", () => {
      for (const run of runs.values()) {
        if (run.socket === socket) run.socket = null;
      }
    });
  });
  server.listen(port, "127.0.0.1");

  let shuttingDown = false;
  let hasFatalError = false;
  const shutdown = async (reason, exitCode, error = null) => {
    if (error) {
      hasFatalError = true;
      logger.error(error);
    }
    if (shuttingDown) return;
    shuttingDown = true;
    const stopped = await Promise.allSettled([...runs.values()].map((run) => stopRun(run)));
    const failures = stopped.filter((result) => result.status === "rejected").map((result) => result.reason);
    if (failures.length) {
      hasFatalError = true;
      logger.error("Debug 插件停止目标失败", new AggregateError(failures, "原目标未全部核实退出"));
    }
    runs.clear();
    for (const client of wss.clients) client.close(1001, reason);
    await new Promise((resolve) => wss.close(resolve));
    await closeHttpServer(server);
    logger.info("Debug 插件已完成关闭", { reason });
    process.exit(hasFatalError ? 1 : exitCode);
  };
  process.once("SIGINT", () => void shutdown("SIGINT", 0));
  process.once("SIGTERM", () => void shutdown("SIGTERM", 0));
  process.on("uncaughtException", (error) => void shutdown("uncaughtException", 1, error));
  process.on("unhandledRejection", (reason) => void shutdown("unhandledRejection", 1, reason));
}

await main();
