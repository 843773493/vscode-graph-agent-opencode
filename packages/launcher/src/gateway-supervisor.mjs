import { spawn } from "node:child_process";
import { once } from "node:events";

const GATEWAY_HOST = "127.0.0.1";
const DEVELOPMENT_GATEWAY_PORT = 8014;
const INSTALLED_GATEWAY_PORT = 8114;
const GATEWAY_READY_TIMEOUT_MS = 90_000;
const GATEWAY_CONNECTION_DRAIN_TIMEOUT_SECONDS = 2;
const GATEWAY_SHUTDOWN_TIMEOUT_MS = 10_000;
function forwardedSignals(platform) {
  return platform === "win32"
    ? ["SIGINT", "SIGTERM", "SIGBREAK"]
    : ["SIGINT", "SIGTERM", "SIGHUP"];
}

export function gatewayEndpoint(distribution) {
  const port =
    distribution === "source-development"
      ? DEVELOPMENT_GATEWAY_PORT
      : INSTALLED_GATEWAY_PORT;
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
  const endpoint = gatewayEndpoint(runtime.distribution);
  return {
    ...baseEnvironment,
    BOXTEAM_DISTRIBUTION: runtime.distribution,
    BOXTEAM_RUNTIME_MANIFEST: runtime.manifestPath,
    BOXTEAM_PROJECT_ROOT: runtime.applicationRoot,
    BOXTEAM_GATEWAY_URL: endpoint.url,
    BOXTEAM_NODE_BIN: runtime.nodeExecutable,
    BOXTEAM_PYTHON_BIN: runtime.pythonExecutable,
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

export function spawnGateway({
  runtime,
  environment,
  spawnImpl = spawn,
  platform = process.platform,
}) {
  const endpoint = gatewayEndpoint(runtime.distribution);
  return spawnImpl(
    runtime.pythonExecutable,
    [
      "-m",
      "uvicorn",
      "app.gateway.main:app",
      "--host",
      endpoint.host,
      "--port",
      String(endpoint.port),
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
  const endpoint = gatewayEndpoint(runtime.distribution);
  stdout.write(
    `BoxTeam ${runtime.version} 正在启动 ` +
      `(distribution=${runtime.distribution})\n`,
  );
  stdout.write(`Gateway: ${endpoint.url}\n`);
  stdout.write(`Python: ${runtime.pythonExecutable}\n`);
  stdout.write(`Node: ${runtime.nodeExecutable}\n`);

  const child = spawnGateway({ runtime, environment, spawnImpl });
  const removeOutputForwarding = forwardGatewayOutput(child, stdout, stderr);
  const removeSignalHandlers = installSignalForwarding(
    child,
    processObject,
    process.platform,
    { stderr },
  );
  const exitResult = once(child, "exit");
  const closeResult = once(child, "close");
  try {
    await Promise.race([
      waitForGateway({
        fetchImpl,
        url: `${endpoint.url}/api/gateway/health`,
      }),
      exitResult.then(([code, signal]) => {
        throw new Error(
          `Gateway 就绪前退出: exit=${String(code)} signal=${String(signal)}`,
        );
      }),
    ]);
    stdout.write(`Gateway 已就绪: ${endpoint.url}\n`);
    if (openBrowser) {
      void openGatewayBrowser({
        spawnImpl,
        url: endpoint.url,
        stderr,
      });
    }
    const [code, signal] = await closeResult;
    if (signal) {
      return 128;
    }
    return typeof code === "number" ? code : 1;
  } catch (error) {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
    }
    throw error;
  } finally {
    removeOutputForwarding();
    removeSignalHandlers();
  }
}
