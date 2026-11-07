import { spawn } from "node:child_process";
import { once } from "node:events";
import net from "node:net";
import { setTimeout as sleep } from "node:timers/promises";
import {
  createGatewaySupervisorControl,
  gatewaySupervisorControlAddress,
  requestGatewaySupervisorControl,
} from "./control-channel.mjs";
import { createGatewayPublicListener } from "./public-listener.mjs";
import { closeServer, listenServer } from "./server-lifetime.mjs";

const GATEWAY_HOST = "127.0.0.1";
const GATEWAY_PORT = 8014;
const RELEASE_DEFAULT_BACKEND_PORT = 8010;
// TODO: Windows 嵌入式 Python 冷启动可能超过 POSIX 默认窗口。
const GATEWAY_READY_TIMEOUT_MS =
  process.platform === "win32" ? 180_000 : 90_000;
const GATEWAY_CONNECTION_DRAIN_TIMEOUT_SECONDS = 2;
// 管理停机总期限120秒，另给普通HTTP连接drain及常见收尾留余量。
export const GATEWAY_SHUTDOWN_TIMEOUT_MS = 150_000;

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
  signal,
}) {
  if (typeof url !== "string" || url.length === 0) {
    throw new TypeError("Gateway 健康检查 URL 必须是非空字符串");
  }
  signal?.throwIfAborted();
  const deadlineMonotonicMs = performance.now() + timeoutMs;
  let lastError = null;
  while (performance.now() < deadlineMonotonicMs) {
    signal?.throwIfAborted();
    const remainingMs = deadlineMonotonicMs - performance.now();
    if (remainingMs <= 0) break;
    let ready = false;
    const requestController = new AbortController();
    const requestTimer = setTimeout(() => {
      requestController.abort(new Error("Gateway 健康请求超过就绪期限"));
    }, Math.ceil(remainingMs));
    try {
      const response = await fetchImpl(url, {
        signal: signal === undefined
          ? requestController.signal
          : AbortSignal.any([signal, requestController.signal]),
      });
      // 就绪只观察状态码；持续输出的正文也必须释放。
      await response.body?.cancel();
      if (performance.now() >= deadlineMonotonicMs) {
        lastError = new Error("Gateway 健康响应超过就绪期限");
      } else if (response.ok) {
        ready = true;
      } else {
        lastError = new Error(`HTTP ${response.status} ${response.statusText}`);
      }
    } catch (error) {
      lastError = error;
    } finally {
      clearTimeout(requestTimer);
      requestController.abort();
    }
    signal?.throwIfAborted();
    if (ready) return;
    const retryDelayMs = Math.min(intervalMs, Math.ceil(deadlineMonotonicMs - performance.now()));
    if (retryDelayMs > 0) {
      try {
        await sleep(retryDelayMs, undefined, { signal });
      } catch (error) {
        signal?.throwIfAborted();
        throw error;
      }
    }
  }
  throw new Error(
    `Gateway 在 ${timeoutMs}ms 内未就绪: ${url}: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
    { cause: lastError },
  );
}

export function gatewayEnvironment(runtime, baseEnvironment) {
  const endpoint = gatewayEndpoint(runtime.distribution, baseEnvironment);
  const startup = gatewayStartupContract(baseEnvironment);
  const defaultBackendPort =
    runtime.distribution === "source-development"
      ? baseEnvironment.BOXTEAM_DEFAULT_BACKEND_PORT?.trim()
      : String(RELEASE_DEFAULT_BACKEND_PORT);
  const environment = { ...baseEnvironment };
  // Gateway 输出经 Launcher 的管道转存，不继承 Launcher 文件描述符的证明。
  delete environment.BOXTEAM_RUNTIME_LOG_SINK;
  return {
    ...environment,
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
      "app.gateway.server.runner",
      "app.gateway.main:app",
      "--host",
      endpoint.host,
      "--port",
      String(listenPort),
      "--shutdown-on-stdin-eof",
      "--timeout-graceful-shutdown",
      String(GATEWAY_CONNECTION_DRAIN_TIMEOUT_SECONDS),
    ],
    {
      cwd: runtime.applicationRoot,
      env: gatewayEnvironment(runtime, environment),
      stdio: ["pipe", "pipe", "pipe"],
      // POSIX 终端会把 Ctrl+C 发给整个前台进程组。让 Gateway 进入独立
      // 进程组后，由 Launcher 成为唯一信号所有者并只转发一次。
      detached: platform !== "win32",
    },
  );
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

function requestGatewayShutdown(child, signal = "SIGTERM", platform = process.platform) {
  if (platform === "win32") {
    // Windows 的 child.kill(SIGTERM) 是硬终止；EOF 由正式 runner 转入同一关闭流程。
    if (!child.stdin.writableEnded) child.stdin.end();
  } else child.kill(signal);
}

async function waitForChildClose(childState, timeoutMs) {
  const controller = new AbortController();
  try {
    return await Promise.race([
      childState.closeResult.then(() => true),
      sleep(timeoutMs, false, { signal: controller.signal }),
    ]);
  } finally { controller.abort(); }
}

export function stopGatewayChild(childState, signal = "SIGTERM", { timeoutMs = GATEWAY_SHUTDOWN_TIMEOUT_MS, forceTimeoutMs = 5_000 } = {}) {
  // 候选取消、显式停止及 finally 共用同一次关闭，不重复发送 POSIX 信号。
  childState.stopResult ??= (async () => {
    try {
      if (childState.child.exitCode === null && childState.child.signalCode === null) {
        requestGatewayShutdown(childState.child, signal);
      }
      if (!await waitForChildClose(childState, timeoutMs)) {
        childState.logger.warning(`Gateway 未在 ${timeoutMs}ms 内退出，发送 SIGKILL`);
        childState.child.kill("SIGKILL");
        if (!await waitForChildClose(childState, forceTimeoutMs)) {
          throw new Error(`Gateway 强制终止后仍未核实退出: pid=${childState.child.pid}`);
        }
      }
      childState.removeOutputForwarding();
    } finally { childState.removeSignalHandlers(); }
  })();
  return childState.stopResult;
}

export function requestGatewayHandoff({
  boxteamHome,
  environment = process.env,
  timeoutMs = 120_000,
}) {
  const address = gatewaySupervisorControlAddress(boxteamHome);
  if (address === null) return Promise.resolve({ handled: false });
  const startup = gatewayStartupContract(environment);
  if (startup.loadedSource !== "pending") {
    throw new Error("Gateway handoff 请求必须携带 pending 启动契约");
  }
  return requestGatewaySupervisorControl({
    address,
    timeoutMs,
    payload: {
      type: "gateway_pending_handoff",
      candidate_ref: startup.candidateRef,
      target_generation: startup.generation,
      fencing_token: startup.fencingToken,
    },
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

export function installSignalForwarding(onStop, processObject = process, platform = process.platform) {
  const listeners = new Map();
  let forwarded = false;
  for (const signal of forwardedSignals(platform)) {
    const listener = () => {
      if (forwarded) return;
      forwarded = true;
      onStop(signal);
    };
    processObject.on(signal, listener);
    listeners.set(signal, listener);
  }
  return () => {
    for (const [signal, listener] of listeners) processObject.off(signal, listener);
  };
}

export async function openGatewayBrowser({
  spawnImpl = spawn,
  platform = process.platform,
  url,
  logger,
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
    logger.warning(
      `无法自动打开浏览器（exit=${String(code)}），请访问 ${url}`,
    );
  }
}

export async function superviseGateway({
  runtime,
  environment,
  openBrowser = true,
  spawnImpl = spawn,
  fetchImpl = fetch,
  logger,
  gatewayStdout = process.stdout,
  gatewayStderr = process.stderr,
  onRuntimeLogging,
  authorizeStop,
  processObject = process,
}) {
  const endpoint = gatewayEndpoint(runtime.distribution, environment);
  // 只有源码开发需要用稳定代理完成 generation handoff；发行版让 Gateway
  // 直接占用公开端口，避免 Launcher 退出后留下不可访问的孤儿 Gateway。
  const gatewayOwnsPublicPort = runtime.distribution !== "source-development";
  logger.info(
    `BoxTeam ${runtime.version} 正在启动 ` +
      `(distribution=${runtime.distribution})`,
  );
  logger.info(`Gateway: ${endpoint.url}`);
  logger.info(`Python: ${runtime.pythonExecutable}`);
  logger.info(`Node: ${runtime.nodeExecutable}`);

  const publicListener = gatewayOwnsPublicPort
    ? null
    : createGatewayPublicListener({
        host: endpoint.host,
        port: endpoint.port,
      });
  if (publicListener !== null) await publicListener.listen();

  const activeEnvironment = activeGatewayEnvironment(environment);
  const controlAddress = gatewaySupervisorControlAddress(environment.BOXTEAM_HOME);
  let currentChild = null;
  let startingChild = null;
  let handoffChain = Promise.resolve();
  let control = null;
  let stopping = false;
  let notifyStopFailure;
  const stopFailure = new Promise(resolve => { notifyStopFailure = resolve; });

  const stopChildren = (signal = "SIGTERM") => {
    if (!stopping) {
      stopping = true;
      startingChild?.readinessController.abort(new Error("Gateway 正在停止，取消候选就绪等待"));
      const children = [...new Set([currentChild, startingChild].filter(child => child !== null))];
      handoffChain = Promise.allSettled([handoffChain, ...children.map(child => stopGatewayChild(child, signal))]).then(results => {
        const failures = [...new Set(results.filter(result => result.status === "rejected").map(result => result.reason))];
        if (failures.length === 1) throw failures[0];
        if (failures.length > 1) throw new AggregateError(failures, "Gateway 交接及停止失败", { cause: failures[0] });
      });
      void handoffChain.catch(notifyStopFailure);
    }
    return handoffChain;
  };

  const startChild = async (
    childEnvironment,
    { installSignalHandlers = true } = {},
  ) => {
    if (stopping) throw new Error("Gateway supervisor 正在停止，不再启动候选");
    const childPort = gatewayOwnsPublicPort
      ? endpoint.port
      : await allocateGatewayChildPort(endpoint.host);
    if (stopping) throw new Error("Gateway supervisor 正在停止，不再启动候选");
    const managedEnvironment = { ...childEnvironment };
    delete managedEnvironment.BOXTEAM_LAUNCHER_CONTROL;
    if (controlAddress !== null) {
      managedEnvironment.BOXTEAM_LAUNCHER_CONTROL = JSON.stringify({
        address: controlAddress, pid: process.pid,
      });
    }
    const child = spawnGateway({
      runtime,
      environment: managedEnvironment,
      port: childPort,
      spawnImpl,
    });
    const removeOutputForwarding = forwardGatewayOutput(child, gatewayStdout, gatewayStderr);
    const removeSignalHandlers = installSignalHandlers
      ? installSignalForwarding(signal => { stopChildren(signal); }, processObject)
      : () => {};
    const exitResult = once(child, "exit");
    const closeResult = once(child, "close");
    const readinessController = new AbortController();
    const childState = { child, childPort, closeResult, removeOutputForwarding, removeSignalHandlers, readinessController, logger };
    startingChild = childState;
    try {
      await Promise.race([
        waitForGateway({
          fetchImpl,
          url: `http://${endpoint.host}:${childPort}/api/gateway/health`,
          signal: readinessController.signal,
        }),
        exitResult.then(([code, signal]) => {
          throw new Error(
            `Gateway 就绪前退出: exit=${String(code)} signal=${String(signal)}`,
          );
        }),
      ]).finally(() => readinessController.abort());
      logger.info(`Gateway 已就绪: ${endpoint.url}`);
      return childState;
    } catch (error) {
      try { await stopGatewayChild(childState); }
      catch (cleanupError) { throw new AggregateError([error, cleanupError], "Gateway 启动及关闭均失败", { cause: error }); }
      throw error;
    } finally {
      if (startingChild === childState) startingChild = null;
    }
  };

  try {
    const handleHandoff = async (payload) => {
      if (publicListener === null) {
        throw new Error("直接监听公开端口的 Gateway 不支持 generation handoff");
      }
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
        logger.warning(
          `Gateway pending generation handoff 失败，继续使用旧 active: ${String(error)}`,
        );
        return { accepted: false, error: String(error) };
      }
      const previous = currentChild;
      previous.removeSignalHandlers();
      replacement.removeSignalHandlers = installSignalForwarding(
        signal => { stopChildren(signal); },
        processObject,
      );
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

    control =
      controlAddress === null
        ? null
        : createGatewaySupervisorControl({
            address: controlAddress,
            onRuntimeLogging,
            onStop(payload) {
              if (typeof authorizeStop !== "function") throw new Error("Launcher 未提供停止授权入口");
              authorizeStop(payload);
              // 同时关闭在途候选和 active，不等待候选180秒就绪期限；初始启动也可锁定停止意图。
              stopChildren();
              return { accepted: true, pid: process.pid };
            },
            onHandoff(payload) {
              if (stopping) throw new Error("Gateway supervisor 正在停止，不再接受交接");
              handoffChain = handoffChain.then(
                () => handleHandoff(payload),
                () => handleHandoff(payload),
              );
              return handoffChain;
            },
          });
    if (control !== null) await control.listen();

    const startup = gatewayStartupContract(environment);
    let initialChild;
    try {
      initialChild = await startChild(environment);
    } catch (error) {
      if (stopping) return 0;
      if (startup.loadedSource !== "pending") throw error;
      logger.warning(
        `Gateway pending generation 未就绪，回退 active snapshot: ${String(error)}`,
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
      publicListener.setTarget({ host: endpoint.host, port: initialChild.childPort });
    }
    if (openBrowser) {
      void openGatewayBrowser({ spawnImpl, url: endpoint.url, logger });
    }

    while (true) {
      const observedChild = currentChild;
      if (observedChild === null) {
        throw new Error("Gateway supervisor active generation 丢失");
      }
      const [code, signal] = await Promise.race([observedChild.closeResult, stopFailure.then(error => { throw error; })]);
      observedChild.removeOutputForwarding();
      observedChild.removeSignalHandlers();
      if (currentChild !== observedChild) continue;
      return signal ? 128 : typeof code === "number" ? code : 1;
    }
  } finally {
    const failures = [];
    for (const cleanup of [
      () => stopChildren(),
      () => control?.close(),
      () => publicListener?.close(),
    ]) {
      try { await cleanup(); }
      catch (error) { if (!failures.includes(error)) failures.push(error); }
    }
    if (failures.length === 1) throw failures[0];
    if (failures.length > 1) throw new AggregateError(failures, "Gateway 停止及资源释放失败", { cause: failures[0] });
  }
}
