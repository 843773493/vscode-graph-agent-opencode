import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { chmodSync, mkdirSync, rmSync } from "node:fs";
import {
  request as httpRequest,
  createServer as createHttpServer,
} from "node:http";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";

const GATEWAY_HOST = "127.0.0.1";
const GATEWAY_PORT = 8014;
const RELEASE_DEFAULT_BACKEND_PORT = 8010;
// TODO: Windows 嵌入式 Python 冷启动可能超过 POSIX 默认窗口。
const GATEWAY_READY_TIMEOUT_MS =
  process.platform === "win32" ? 180_000 : 90_000;
const GATEWAY_CONNECTION_DRAIN_TIMEOUT_SECONDS = 2;
const GATEWAY_SHUTDOWN_TIMEOUT_MS = 10_000;

function resolveDevelopmentGatewayPort(environment) {
  const rawValue = environment?.BOXTEAM_GATEWAY_PORT?.trim() ?? "";
  if (rawValue === "") return GATEWAY_PORT;
  const port = Number(rawValue);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new Error(
      `BOXTEAM_GATEWAY_PORT 必须是 1 到 65535 的整数，实际为 ${rawValue}`,
    );
  }
  return port;
}

function forwardedSignals(platform) {
  return platform === "win32"
    ? ["SIGINT", "SIGTERM", "SIGBREAK"]
    : ["SIGINT", "SIGTERM", "SIGHUP"];
}

export function gatewayEndpoint(distribution, environment = process.env) {
  const port =
    distribution === "source-development"
      ? resolveDevelopmentGatewayPort(environment)
      : GATEWAY_PORT;
  return Object.freeze({
    host: GATEWAY_HOST,
    port,
    url: `http://${GATEWAY_HOST}:${port}`,
  });
}

export async function waitForGateway({
  fetchImpl = fetch,
  url,
  timeoutMs = GATEWAY_READY_TIMEOUT_MS,
  intervalMs = 250,
}) {
  if (typeof url !== "string" || url.length === 0) {
    throw new TypeError("Gateway 健康检查 URL 必须是非空字符串");
  }
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetchImpl(url);
      if (response.ok) return;
      lastError = new Error(`HTTP ${response.status} ${response.statusText}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(
    `Gateway 在 ${timeoutMs}ms 内未就绪: ${url}: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
  );
}

export function gatewayEnvironment(runtime, baseEnvironment) {
  const endpoint = gatewayEndpoint(runtime.distribution, baseEnvironment);
  const startup = gatewayStartupContract(baseEnvironment);
  const defaultBackendPort =
    runtime.distribution === "source-development"
      ? baseEnvironment.BOXTEAM_DEFAULT_BACKEND_PORT?.trim()
      : String(RELEASE_DEFAULT_BACKEND_PORT);
  return {
    ...baseEnvironment,
    BOXTEAM_DISTRIBUTION: runtime.distribution,
    BOXTEAM_RUNTIME_MANIFEST: runtime.manifestPath,
    BOXTEAM_PROJECT_ROOT: runtime.applicationRoot,
    BOXTEAM_GATEWAY_URL: endpoint.url,
    BOXTEAM_NODE_BIN: runtime.nodeExecutable,
    BOXTEAM_PYTHON_BIN: runtime.pythonExecutable,
    ...(defaultBackendPort === undefined || defaultBackendPort === ""
      ? {}
      : { BOXTEAM_DEFAULT_BACKEND_PORT: defaultBackendPort }),
    ...startup.environment,
    ...(runtime.webAssets === null
      ? {}
      : { BOXTEAM_WEB_ASSETS: runtime.webAssets }),
    ...(runtime.chromiumExecutable === null
      ? {}
      : {
          PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH: runtime.chromiumExecutable,
        }),
  };
}

function requiredStartupValue(value, label) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new TypeError(`${label} 必须是非空字符串`);
  }
  return value.trim();
}

export function gatewayStartupContract(environment = process.env) {
  const candidateRef = environment.BOXTEAM_CONFIG_CANDIDATE_REF?.trim() ?? "";
  const generation = environment.BOXTEAM_CONFIG_GENERATION?.trim() ?? "";
  const fencingToken = environment.BOXTEAM_CONFIG_FENCING_TOKEN?.trim() ?? "";
  if (candidateRef === "") {
    if (generation !== "" || fencingToken !== "") {
      throw new Error(
        "Gateway 普通恢复不能携带不完整的 pending candidate 启动契约",
      );
    }
    return Object.freeze({
      loadedSource: "active",
      candidateRef: null,
      generation: null,
      fencingToken: null,
      environment: Object.freeze({}),
    });
  }
  return Object.freeze({
    loadedSource: "pending",
    candidateRef: requiredStartupValue(candidateRef, "Gateway candidate_ref"),
    generation: requiredStartupValue(generation, "Gateway generation"),
    fencingToken: requiredStartupValue(fencingToken, "Gateway fencing token"),
    environment: Object.freeze({
      BOXTEAM_CONFIG_CANDIDATE_REF: candidateRef,
      BOXTEAM_CONFIG_GENERATION: generation,
      BOXTEAM_CONFIG_FENCING_TOKEN: fencingToken,
    }),
  });
}

export function buildGatewayPendingEnvironment(baseEnvironment, intent) {
  if (intent === null || typeof intent !== "object" || Array.isArray(intent)) {
    throw new TypeError("Gateway pending restart intent 必须是对象");
  }
  return {
    ...baseEnvironment,
    BOXTEAM_CONFIG_CANDIDATE_REF: requiredStartupValue(
      intent.candidate_ref,
      "Gateway candidate_ref",
    ),
    BOXTEAM_CONFIG_GENERATION: requiredStartupValue(
      intent.target_generation,
      "Gateway generation",
    ),
    BOXTEAM_CONFIG_FENCING_TOKEN: requiredStartupValue(
      intent.fencing_token,
      "Gateway fencing token",
    ),
  };
}

export function spawnGateway({
  runtime,
  environment,
  port = null,
  spawnImpl = spawn,
  platform = process.platform,
}) {
  const endpoint = gatewayEndpoint(runtime.distribution, environment);
  const listenPort = port ?? endpoint.port;
  return spawnImpl(
    runtime.pythonExecutable,
    [
      "-m",
      "uvicorn",
      "app.gateway.main:app",
      "--host",
      endpoint.host,
      "--port",
      String(listenPort),
      "--timeout-graceful-shutdown",
      String(GATEWAY_CONNECTION_DRAIN_TIMEOUT_SECONDS),
    ],
    {
      cwd: runtime.applicationRoot,
      env: gatewayEnvironment(runtime, environment),
      stdio: ["inherit", "pipe", "pipe"],
      // POSIX 终端会把 Ctrl+C 发给整个前台进程组。让 Gateway 进入独立
      // 进程组后，由 Launcher 成为唯一信号所有者并只转发一次。
      detached: platform !== "win32",
    },
  );
}

function listenServer(server, options) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolve(server);
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(options);
  });
}

function closeServer(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) reject(error);
      else resolve();
    });
  });
}

async function allocateGatewayChildPort(host) {
  const probe = net.createServer();
  await listenServer(probe, { host, port: 0 });
  const address = probe.address();
  if (address === null || typeof address === "string") {
    await closeServer(probe);
    throw new Error("无法取得 Gateway 子进程临时端口");
  }
  const port = address.port;
  await closeServer(probe);
  return port;
}

function gatewayTargetUrl(target) {
  return `http://${target.host}:${target.port}`;
}

function writeUnavailableResponse(response) {
  response.writeHead(503, {
    "cache-control": "no-store",
    "content-type": "text/plain; charset=utf-8",
  });
  response.end("Gateway supervisor 尚未绑定 active generation\n");
}

function proxyHttpRequest(request, response, target) {
  if (target === null) {
    writeUnavailableResponse(response);
    return;
  }
  const upstream = httpRequest(
    {
      hostname: target.host,
      port: target.port,
      method: request.method,
      path: request.url,
      headers: request.headers,
      agent: false,
    },
    (upstreamResponse) => {
      response.writeHead(
        upstreamResponse.statusCode ?? 502,
        upstreamResponse.headers,
      );
      upstreamResponse.pipe(response);
    },
  );
  upstream.once("error", (error) => {
    if (!response.headersSent) {
      response.writeHead(502, {
        "content-type": "text/plain; charset=utf-8",
      });
    }
    response.end(`Gateway generation 代理失败: ${error.message}\n`);
  });
  request.pipe(upstream);
}

function proxyGatewayUpgrade(request, clientSocket, head, target) {
  if (target === null) {
    clientSocket.end(
      "HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n",
    );
    return;
  }
  const upstream = httpRequest({
    hostname: target.host,
    port: target.port,
    method: request.method,
    path: request.url,
    headers: request.headers,
    agent: false,
  });
  const closeSockets = () => {
    clientSocket.destroy();
    upstream.destroy();
  };
  upstream.once("upgrade", (upstreamResponse, upstreamSocket, upstreamHead) => {
    const responseLines = [
      `HTTP/1.1 ${upstreamResponse.statusCode ?? 101} ${upstreamResponse.statusMessage ?? "Switching Protocols"}`,
    ];
    for (
      let index = 0;
      index < upstreamResponse.rawHeaders.length;
      index += 2
    ) {
      responseLines.push(
        `${upstreamResponse.rawHeaders[index]}: ${upstreamResponse.rawHeaders[index + 1]}`,
      );
    }
    clientSocket.write(`${responseLines.join("\r\n")}\r\n\r\n`);
    if (upstreamHead.length > 0) clientSocket.write(upstreamHead);
    clientSocket.pipe(upstreamSocket);
    upstreamSocket.pipe(clientSocket);
    clientSocket.once("error", closeSockets);
    upstreamSocket.once("error", closeSockets);
  });
  upstream.once("response", (upstreamResponse) => {
    upstreamResponse.resume();
    clientSocket.destroy();
  });
  upstream.once("error", closeSockets);
  upstream.end(head);
}

export function createGatewayPublicListener({ host, port }) {
  let target = null;
  const server = createHttpServer((request, response) => {
    proxyHttpRequest(request, response, target);
  });
  server.on("upgrade", (request, clientSocket, head) => {
    proxyGatewayUpgrade(request, clientSocket, head, target);
  });
  return {
    async listen() {
      await listenServer(server, { host, port });
    },
    setTarget(nextTarget) {
      if (
        nextTarget !== null &&
        (typeof nextTarget.host !== "string" ||
          !Number.isSafeInteger(nextTarget.port) ||
          nextTarget.port < 1 ||
          nextTarget.port > 65535)
      ) {
        throw new TypeError("Gateway supervisor target 无效");
      }
      target = nextTarget;
    },
    address() {
      return server.address();
    },
    targetUrl() {
      return target === null ? null : gatewayTargetUrl(target);
    },
    async close() {
      await closeServer(server);
    },
  };
}

function gatewaySupervisorSocketPath(boxteamHome) {
  if (typeof boxteamHome !== "string" || boxteamHome.trim() === "") {
    return null;
  }
  const resolvedHome = path.resolve(boxteamHome);
  const socketPath = path.join(
    resolvedHome,
    "state",
    "gateway-supervisor.sock",
  );
  // Unix domain socket 的路径有系统上限；长测试输出目录或用户目录不能
  // 让 supervisor 在 listen 后因 chmod 找不到实际 socket 而失败。
  if (Buffer.byteLength(socketPath) <= 100) return socketPath;
  const homeDigest = createHash("sha256")
    .update(resolvedHome)
    .digest("hex")
    .slice(0, 32);
  return path.join(tmpdir(), `boxteam-gateway-supervisor-${homeDigest}.sock`);
}

async function stopGatewayChild(childState) {
  if (
    childState.child.exitCode === null &&
    childState.child.signalCode === null
  ) {
    childState.child.kill("SIGTERM");
  }
  const closed = await Promise.race([
    childState.closeResult.then(() => true),
    new Promise((resolve) => {
      const timer = setTimeout(
        () => resolve(false),
        GATEWAY_SHUTDOWN_TIMEOUT_MS,
      );
      timer.unref?.();
    }),
  ]);
  if (!closed) {
    childState.child.kill("SIGKILL");
    await childState.closeResult;
  }
  childState.removeOutputForwarding();
  childState.removeSignalHandlers();
}

export function createGatewaySupervisorControl({ socketPath, onHandoff }) {
  if (typeof socketPath !== "string" || socketPath.length === 0) {
    throw new TypeError("Gateway supervisor control socket 路径无效");
  }
  if (typeof onHandoff !== "function") {
    throw new TypeError("Gateway supervisor control handler 无效");
  }
  const server = net.createServer((socket) => {
    let buffer = "";
    let handled = false;
    socket.on("error", () => {
      // 控制请求方断开时只结束当前请求，不得让 supervisor 进程崩溃。
    });
    socket.on("data", (chunk) => {
      if (handled) return;
      buffer += chunk.toString("utf8");
      if (buffer.length > 64 * 1024) {
        socket.destroy(new Error("Gateway supervisor control 请求过大"));
        return;
      }
      const newlineIndex = buffer.indexOf("\n");
      if (newlineIndex === -1) return;
      handled = true;
      const line = buffer.slice(0, newlineIndex).trim();
      let payload;
      try {
        payload = JSON.parse(line);
      } catch (error) {
        socket.end(`${JSON.stringify({ ok: false, error: String(error) })}\n`);
        return;
      }
      Promise.resolve()
        .then(() => onHandoff(payload))
        .then((data) => {
          socket.end(`${JSON.stringify({ ok: true, data })}\n`);
        })
        .catch((error) => {
          socket.end(
            `${JSON.stringify({
              ok: false,
              error: error instanceof Error ? error.message : String(error),
            })}\n`,
          );
        });
    });
  });
  return {
    async listen() {
      mkdirSync(path.dirname(socketPath), { recursive: true, mode: 0o700 });
      rmSync(socketPath, { force: true });
      await listenServer(server, socketPath);
      if (process.platform !== "win32") chmodSync(socketPath, 0o600);
    },
    async close() {
      await closeServer(server);
      rmSync(socketPath, { force: true });
    },
  };
}

export function requestGatewayHandoff({
  boxteamHome,
  environment = process.env,
  timeoutMs = 120_000,
}) {
  const socketPath = gatewaySupervisorSocketPath(boxteamHome);
  if (socketPath === null) {
    return Promise.resolve({ handled: false });
  }
  const startup = gatewayStartupContract(environment);
  if (startup.loadedSource !== "pending") {
    throw new Error("Gateway handoff 请求必须携带 pending 启动契约");
  }
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let buffer = "";
    let settled = false;
    const timer = setTimeout(() => {
      socket.destroy();
      reject(
        new Error(`Gateway supervisor handoff 在 ${timeoutMs}ms 内未响应`),
      );
    }, timeoutMs);
    timer.unref?.();
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    socket.on("connect", () => {
      socket.write(
        `${JSON.stringify({
          type: "gateway_pending_handoff",
          candidate_ref: startup.candidateRef,
          target_generation: startup.generation,
          fencing_token: startup.fencingToken,
        })}\n`,
      );
    });
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      const newlineIndex = buffer.indexOf("\n");
      if (newlineIndex === -1) return;
      let response;
      try {
        response = JSON.parse(buffer.slice(0, newlineIndex));
      } catch (error) {
        finish(() => reject(error));
        return;
      }
      finish(() => {
        if (!response.ok) {
          reject(new Error(String(response.error ?? "Gateway handoff 失败")));
          return;
        }
        resolve({ handled: true, data: response.data ?? null });
      });
    });
    socket.on("error", (error) => {
      if (error.code === "ENOENT" || error.code === "ECONNREFUSED") {
        finish(() => resolve({ handled: false }));
        return;
      }
      finish(() => reject(error));
    });
  });
}

function activeGatewayEnvironment(environment) {
  const activeEnvironment = { ...environment };
  for (const variable of [
    "BOXTEAM_CONFIG_CANDIDATE_REF",
    "BOXTEAM_CONFIG_GENERATION",
    "BOXTEAM_CONFIG_FENCING_TOKEN",
  ]) {
    delete activeEnvironment[variable];
  }
  return activeEnvironment;
}

export function forwardGatewayOutput(child, stdout, stderr) {
  const stdoutListener = (chunk) => stdout.write(chunk);
  const stderrListener = (chunk) => stderr.write(chunk);
  child.stdout?.on("data", stdoutListener);
  child.stderr?.on("data", stderrListener);
  return () => {
    child.stdout?.off("data", stdoutListener);
    child.stderr?.off("data", stderrListener);
  };
}

export function installSignalForwarding(
  child,
  processObject = process,
  platform = process.platform,
  {
    shutdownTimeoutMs = GATEWAY_SHUTDOWN_TIMEOUT_MS,
    setTimeoutImpl = setTimeout,
    clearTimeoutImpl = clearTimeout,
    stderr = process.stderr,
  } = {},
) {
  const listeners = new Map();
  let forwarded = false;
  let forceTimer = null;
  for (const signal of forwardedSignals(platform)) {
    const listener = () => {
      if (!forwarded && child.exitCode === null && child.signalCode === null) {
        forwarded = true;
        child.kill(signal === "SIGBREAK" ? "SIGTERM" : signal);
        forceTimer = setTimeoutImpl(() => {
          if (child.exitCode !== null || child.signalCode !== null) return;
          stderr.write(
            `boxteam: Gateway 未在 ${shutdownTimeoutMs}ms 内退出，发送 SIGKILL\n`,
          );
          child.kill("SIGKILL");
        }, shutdownTimeoutMs);
        forceTimer?.unref?.();
      }
    };
    processObject.on(signal, listener);
    listeners.set(signal, listener);
  }
  return () => {
    if (forceTimer !== null) clearTimeoutImpl(forceTimer);
    for (const [signal, listener] of listeners) {
      processObject.off(signal, listener);
    }
  };
}

export async function openGatewayBrowser({
  spawnImpl = spawn,
  platform = process.platform,
  url,
  stderr = process.stderr,
}) {
  if (typeof url !== "string" || url.length === 0) {
    throw new TypeError("Gateway 浏览器 URL 必须是非空字符串");
  }
  const command =
    platform === "win32"
      ? ["cmd.exe", ["/d", "/s", "/c", "start", "", url]]
      : platform === "darwin"
        ? ["open", [url]]
        : ["xdg-open", [url]];
  const child = spawnImpl(command[0], command[1], {
    stdio: "ignore",
    detached: false,
  });
  const [code] = await once(child, "exit");
  if (code !== 0) {
    stderr.write(
      `boxteam: 无法自动打开浏览器（exit=${String(code)}），请访问 ${url}\n`,
    );
  }
}

export async function superviseGateway({
  runtime,
  environment,
  openBrowser = true,
  spawnImpl = spawn,
  fetchImpl = fetch,
  stdout = process.stdout,
  stderr = process.stderr,
  processObject = process,
}) {
  const endpoint = gatewayEndpoint(runtime.distribution, environment);
  // 只有源码开发需要用稳定代理完成 generation handoff；发行版让 Gateway
  // 直接占用公开端口，避免 Launcher 退出后留下不可访问的孤儿 Gateway。
  const gatewayOwnsPublicPort = runtime.distribution !== "source-development";
  stdout.write(
    `BoxTeam ${runtime.version} 正在启动 ` +
      `(distribution=${runtime.distribution})\n`,
  );
  stdout.write(`Gateway: ${endpoint.url}\n`);
  stdout.write(`Python: ${runtime.pythonExecutable}\n`);
  stdout.write(`Node: ${runtime.nodeExecutable}\n`);

  const publicListener = gatewayOwnsPublicPort
    ? null
    : createGatewayPublicListener({
        host: endpoint.host,
        port: endpoint.port,
      });
  if (publicListener !== null) await publicListener.listen();

  const activeEnvironment = activeGatewayEnvironment(environment);
  let currentChild = null;
  let handoffChain = Promise.resolve();
  let control = null;

  const startChild = async (
    childEnvironment,
    { installSignalHandlers = true } = {},
  ) => {
    const childPort = gatewayOwnsPublicPort
      ? endpoint.port
      : await allocateGatewayChildPort(endpoint.host);
    const child = spawnGateway({
      runtime,
      environment: childEnvironment,
      port: childPort,
      spawnImpl,
    });
    const removeOutputForwarding = forwardGatewayOutput(child, stdout, stderr);
    const removeSignalHandlers = installSignalHandlers
      ? installSignalForwarding(child, processObject, process.platform, {
          stderr,
        })
      : () => {};
    const exitResult = once(child, "exit");
    const closeResult = once(child, "close");
    try {
      await Promise.race([
        waitForGateway({
          fetchImpl,
          url: `http://${endpoint.host}:${childPort}/api/gateway/health`,
        }),
        exitResult.then(([code, signal]) => {
          throw new Error(
            `Gateway 就绪前退出: exit=${String(code)} signal=${String(signal)}`,
          );
        }),
      ]);
      stdout.write(`Gateway 已就绪: ${endpoint.url}\n`);
      return {
        child,
        childPort,
        closeResult,
        removeOutputForwarding,
        removeSignalHandlers,
      };
    } catch (error) {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill("SIGTERM");
      }
      await closeResult;
      removeOutputForwarding();
      removeSignalHandlers();
      throw error;
    }
  };

  try {
    const startup = gatewayStartupContract(environment);
    let initialChild;
    try {
      initialChild = await startChild(environment);
    } catch (error) {
      if (startup.loadedSource !== "pending") {
        throw error;
      }
      stdout.write(
        `Gateway pending generation 未就绪，回退 active snapshot: ${String(error)}\n`,
      );
      try {
        initialChild = await startChild(activeEnvironment);
      } catch (fallbackError) {
        throw new Error(
          `Gateway pending 启动失败，且 active fallback 也失败: ${String(fallbackError)}`,
          { cause: fallbackError },
        );
      }
    }
    currentChild = initialChild;
    if (publicListener !== null) {
      publicListener.setTarget({
        host: endpoint.host,
        port: initialChild.childPort,
      });
    }
    if (openBrowser) {
      void openGatewayBrowser({
        spawnImpl,
        url: endpoint.url,
        stderr,
      });
    }

    const handleHandoff = async (payload) => {
      if (currentChild === null) {
        throw new Error("Gateway supervisor 当前没有 active generation");
      }
      const pendingEnvironment = buildGatewayPendingEnvironment(
        activeEnvironment,
        payload,
      );
      let replacement;
      try {
        replacement = await startChild(pendingEnvironment, {
          installSignalHandlers: false,
        });
      } catch (error) {
        stdout.write(
          `Gateway pending generation handoff 失败，继续使用旧 active: ${String(error)}\n`,
        );
        return { accepted: false, error: String(error) };
      }
      const previous = currentChild;
      previous.removeSignalHandlers();
      replacement.removeSignalHandlers = installSignalForwarding(
        replacement.child,
        processObject,
        process.platform,
        { stderr },
      );
      if (publicListener === null) {
        throw new Error("直接监听公开端口的 Gateway 不支持 generation handoff");
      }
      publicListener.setTarget({
        host: endpoint.host,
        port: replacement.childPort,
      });
      currentChild = replacement;
      await stopGatewayChild(previous);
      return {
        accepted: true,
        target_generation: payload.target_generation,
      };
    };

    const socketPath = gatewayOwnsPublicPort
      ? null
      : gatewaySupervisorSocketPath(environment.BOXTEAM_HOME);
    control =
      socketPath === null
        ? null
        : createGatewaySupervisorControl({
            socketPath,
            onHandoff(payload) {
              handoffChain = handoffChain.then(
                () => handleHandoff(payload),
                () => handleHandoff(payload),
              );
              return handoffChain;
            },
          });
    if (control !== null) await control.listen();

    while (true) {
      const observedChild = currentChild;
      if (observedChild === null) {
        throw new Error("Gateway supervisor active generation 丢失");
      }
      const [code, signal] = await observedChild.closeResult;
      observedChild.removeOutputForwarding();
      observedChild.removeSignalHandlers();
      if (currentChild !== observedChild) continue;
      return signal ? 128 : typeof code === "number" ? code : 1;
    }
  } finally {
    if (currentChild !== null) {
      await stopGatewayChild(currentChild);
      currentChild = null;
    }
    if (control !== null) await control.close();
    if (publicListener !== null) await publicListener.close();
  }
}
