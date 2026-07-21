import { spawn } from "node:child_process";
import { once } from "node:events";

const GATEWAY_HOST = "127.0.0.1";
const GATEWAY_PORT = 8014;
const GATEWAY_URL = `http://${GATEWAY_HOST}:${GATEWAY_PORT}`;
const FORWARDED_SIGNALS =
  process.platform === "win32"
    ? ["SIGINT", "SIGTERM", "SIGBREAK"]
    : ["SIGINT", "SIGTERM", "SIGHUP"];

export async function waitForGateway({
  fetchImpl = fetch,
  url = `${GATEWAY_URL}/api/gateway/health`,
  timeoutMs = 45_000,
  intervalMs = 250,
}) {
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
  return {
    ...baseEnvironment,
    BOXTEAM_DISTRIBUTION: runtime.distribution,
    BOXTEAM_RUNTIME_MANIFEST: runtime.manifestPath,
    BOXTEAM_PROJECT_ROOT: runtime.applicationRoot,
    BOXTEAM_GATEWAY_URL: GATEWAY_URL,
    BOXTEAM_NODE_BIN: runtime.nodeExecutable,
    BOXTEAM_PYTHON_BIN: runtime.pythonExecutable,
    ...(runtime.webAssets === null
      ? {}
      : { BOXTEAM_WEB_ASSETS: runtime.webAssets }),
    ...(runtime.chromiumExecutable === null
      ? {}
      : {
          PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH:
            runtime.chromiumExecutable,
        }),
  };
}

export function spawnGateway({
  runtime,
  environment,
  spawnImpl = spawn,
}) {
  return spawnImpl(
    runtime.pythonExecutable,
    [
      "-m",
      "uvicorn",
      "app.gateway.main:app",
      "--host",
      GATEWAY_HOST,
      "--port",
      String(GATEWAY_PORT),
    ],
    {
      cwd: runtime.applicationRoot,
      env: gatewayEnvironment(runtime, environment),
      stdio: "inherit",
    },
  );
}

export function installSignalForwarding(child, processObject = process) {
  const listeners = new Map();
  for (const signal of FORWARDED_SIGNALS) {
    const listener = () => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill(signal === "SIGBREAK" ? "SIGTERM" : signal);
      }
    };
    processObject.on(signal, listener);
    listeners.set(signal, listener);
  }
  return () => {
    for (const [signal, listener] of listeners) {
      processObject.off(signal, listener);
    }
  };
}

export async function openGatewayBrowser({
  spawnImpl = spawn,
  platform = process.platform,
  url = GATEWAY_URL,
  stderr = process.stderr,
}) {
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
  stdout.write(
    `BoxTeam ${runtime.version} 正在启动 ` +
      `(distribution=${runtime.distribution})\n`,
  );
  stdout.write(`Gateway: ${GATEWAY_URL}\n`);
  stdout.write(`Python: ${runtime.pythonExecutable}\n`);
  stdout.write(`Node: ${runtime.nodeExecutable}\n`);

  const child = spawnGateway({ runtime, environment, spawnImpl });
  const removeSignalHandlers = installSignalForwarding(child, processObject);
  try {
    await Promise.race([
      waitForGateway({ fetchImpl }),
      once(child, "exit").then(([code, signal]) => {
        throw new Error(
          `Gateway 就绪前退出: exit=${String(code)} signal=${String(signal)}`,
        );
      }),
    ]);
    stdout.write(`Gateway 已就绪: ${GATEWAY_URL}\n`);
    if (openBrowser) {
      void openGatewayBrowser({ spawnImpl, stderr });
    }
    const [code, signal] = await once(child, "exit");
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
    removeSignalHandlers();
  }
}

export { GATEWAY_URL };
