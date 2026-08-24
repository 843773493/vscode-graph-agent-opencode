import path from "node:path";
import os from "node:os";
import {
  existsSync,
  copyFileSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { once } from "node:events";

import {
  spawnDetachedProcess,
  terminateDetachedProcess,
  terminateProcessWithEscalation,
} from "../packages/launcher/src/detached-process.mjs";
import {
  resolveServiceLogPath,
  SERVICE_LOG_CAPTURED_ENV,
} from "../packages/launcher/src/service-log.mjs";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const webRoot = path.join(projectRoot, "src", "clients", "web");
const terminalFrontendRoot = path.join(
  projectRoot,
  "src",
  "workspace-services",
  "terminal",
  "client",
);
const browserFrontendRoot = path.join(
  projectRoot,
  "src",
  "workspace-services",
  "browser",
  "client",
);
const rawPortOffset = process.env.BOXTEAM_DEV_PORT_OFFSET?.trim() ?? "";

function parsePortOffset(value) {
  if (value === "") return 0;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > 57000) {
    throw new Error(
      `BOXTEAM_DEV_PORT_OFFSET 必须是 0 到 57000 的整数，实际为 ${value}`,
    );
  }
  return parsed;
}

const portOffset = parsePortOffset(rawPortOffset);
const defaultBoxteamHome =
  portOffset === 0 ? ".boxteams-dev" : `.boxteams-dev-${portOffset}`;
const boxteamHome = path.resolve(
  process.env.BOXTEAM_HOME ?? path.join(os.homedir(), defaultBoxteamHome),
);
const defaultWorkspaceRoot = path.resolve(
  process.env.BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT ??
    path.join(boxteamHome, "boxteam_workspace"),
);
const launcherEntry = path.join(
  projectRoot,
  "packages",
  "launcher",
  "bin",
  "boxteam.mjs",
);
const pythonBin = path.resolve(
  process.env.BOXTEAM_PYTHON_BIN ??
    path.join(
      projectRoot,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    ),
);
const nodeBin =
  process.env.NODE_BIN ?? (process.platform === "win32" ? "node.exe" : "node");
const cliArguments = process.argv.slice(2);
const onlyLaunch = cliArguments.includes("--only-launch");
const serviceArgumentIndex = cliArguments.findIndex(
  (argument) => argument === "--service" || argument.startsWith("--service="),
);
const serviceArgument =
  serviceArgumentIndex === -1
    ? null
    : cliArguments[serviceArgumentIndex] === "--service"
      ? cliArguments[serviceArgumentIndex + 1] ?? null
      : cliArguments[serviceArgumentIndex].slice("--service=".length);
const serviceMode = serviceArgument ?? "all";
if (!["all", "backend", "gateway", "web"].includes(serviceMode)) {
  throw new Error(
    `--service 必须是 all、backend、gateway 或 web，实际为 ${String(serviceMode)}`,
  );
}
if (onlyLaunch && serviceMode !== "all") {
  throw new Error("--only-launch 只能与 --service=all 一起使用");
}
const restartDelayArgument = cliArguments.find((argument) =>
  argument.startsWith("--restart-delay-ms="),
);
const restartDelayMs = restartDelayArgument
  ? Number(restartDelayArgument.slice("--restart-delay-ms=".length))
  : 0;
if (
  !Number.isInteger(restartDelayMs) ||
  restartDelayMs < 0 ||
  restartDelayMs > 10_000
) {
  throw new Error(
    `--restart-delay-ms 必须是 0 到 10000 的整数，实际为 ${String(restartDelayMs)}`,
  );
}
const detachedReadyFile = process.env.BOXTEAM_DEV_READY_FILE ?? null;
const host = "127.0.0.1";
const basePorts = {
  backend: 8010,
  frontend: 8011,
  terminalFrontend: 8013,
  gateway: 8014,
  browserFrontend: 8016,
  backendDebug: 8002,
};
const ports = Object.fromEntries(
  Object.entries(basePorts).map(([name, port]) => [name, port + portOffset]),
);

function requirePath(targetPath, label) {
  if (!existsSync(targetPath)) {
    throw new Error(`${label}不存在: ${targetPath}`);
  }
}

function selectedPorts() {
  if (serviceMode === "backend") return [ports.backend];
  if (serviceMode === "gateway") return [ports.gateway];
  if (serviceMode === "web") return [ports.frontend];
  return Object.values(ports);
}

function spawnProcess(command, args, cwd, environment) {
  return Bun.spawn([command, ...args], {
    cwd,
    env: environment,
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
}

function installDevelopmentConfiguration(environment) {
  const result = Bun.spawnSync(
    [
      pythonBin,
      "-m",
      "configs.boxteam",
      "install-source-development",
      "--project-root",
      projectRoot,
    ],
    {
      cwd: projectRoot,
      env: environment,
      stdout: "inherit",
      stderr: "inherit",
    },
  );
  if (result.exitCode !== 0) {
    throw new Error(
      `源码开发配置安装失败: exit=${String(result.exitCode)}`,
    );
  }
}

function installNodeDebugFixture() {
  const sourcePath = path.join(
    projectRoot,
    "scripts",
    "debug-fixtures",
    "node-debug-fixture.mjs",
  );
  const targetPath = path.join(
    defaultWorkspaceRoot,
    "debug",
    "node-debug-fixture.mjs",
  );
  requirePath(sourcePath, "Node 调试测试脚本");
  if (!existsSync(targetPath)) {
    mkdirSync(path.dirname(targetPath), { recursive: true });
    copyFileSync(sourcePath, targetPath);
  }
}

function writeDevelopmentManifest() {
  const runtimeRoot = path.join(projectRoot, "out", "development-runtime");
  mkdirSync(runtimeRoot, { recursive: true });
  const manifestPath = path.join(runtimeRoot, "runtime-manifest.json");
  writeFileSync(
    manifestPath,
    `${JSON.stringify(
      {
        schema_version: 1,
        distribution: "source-development",
        version: "0.1.0",
        python_executable: pythonBin,
        application_root: projectRoot,
        config_resources: {
          gateway_inline: path.join(projectRoot, "configs", "gateway_inline.jsonc"),
          gateway_schema: path.join(projectRoot, "configs", "gateway_schema.jsonc"),
          workspace_inline: path.join(projectRoot, "configs", "workspace_inline.jsonc"),
          workspace_schema: path.join(projectRoot, "configs", "workspace_schema.jsonc"),
        },
        skill_resources: path.join(projectRoot, "resources", "skills"),
        web_assets: null,
        chromium_executable:
          process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? null,
        node: {
          source: "launcher",
          executable: null,
        },
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
  return manifestPath;
}

async function waitForHttpOk(url, label) {
  const timeoutMs = 90_000;
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        process.stdout.write(`[dev] ${label} ready: ${url}\n`);
        return;
      }
      lastError = new Error(`HTTP ${response.status} ${response.statusText}`);
    } catch (error) {
      lastError = error;
    }
    await Bun.sleep(250);
  }
  throw new Error(
    `${label}在 ${timeoutMs}ms 内未就绪: ${url}: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
  );
}

async function waitForDetachedReady(child, readyFile, logPath) {
  const timeoutMs = 120_000;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(
        `detached dev manager 就绪前退出: exit=${String(child.exitCode)} ` +
          `signal=${String(child.signalCode)}，日志: ${logPath}`,
      );
    }
    if (existsSync(readyFile)) {
      try {
        const payload = JSON.parse(readFileSync(readyFile, "utf8"));
        if (payload.pid === child.pid) return;
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
      }
    }
    await Bun.sleep(250);
  }
  throw new Error(
    `detached dev manager 在 ${timeoutMs}ms 内未就绪，日志: ${logPath}`,
  );
}

async function launchDetachedManager() {
  const runtimeRoot = path.join(projectRoot, "out", "development-runtime");
  mkdirSync(runtimeRoot, { recursive: true });
  const readyFile = path.join(runtimeRoot, "detached-ready.json");
  const logPath = resolveServiceLogPath(boxteamHome, process.env);
  rmSync(readyFile, { force: true });

  const child = spawnDetachedProcess({
    command: process.execPath,
    args: [path.join(projectRoot, "scripts", "dev.mjs")],
    cwd: projectRoot,
    environment: {
      ...process.env,
      BOXTEAM_DEV_READY_FILE: readyFile,
    },
    logPath,
  });
  if (!Number.isInteger(child.pid)) {
    throw new Error(`无法取得 detached dev manager pid，日志: ${logPath}`);
  }

  const spawnError = once(child, "error").then(([error]) => {
    throw error;
  });
  try {
    await Promise.race([
      waitForDetachedReady(child, readyFile, logPath),
      spawnError,
    ]);
  } catch (error) {
    terminateDetachedProcess(child.pid);
    throw error;
  }
  child.unref();
  process.stdout.write(
    `[dev] detached services ready: pid=${child.pid}, log=${logPath}\n`,
  );
}

function listenerPidsUnix(targetPort) {
  const lsof = Bun.spawnSync(["lsof", "-ti", `tcp:${targetPort}`], {
    cwd: projectRoot,
    stdout: "pipe",
    stderr: "ignore",
  });
  if (lsof.exitCode !== 0) return [];
  return new TextDecoder()
    .decode(lsof.stdout)
    .trim()
    .split(/\r?\n/)
    .filter((value) => /^\d+$/.test(value));
}

async function cleanDevelopmentPorts() {
  const targetPorts = selectedPorts();
  if (process.platform === "win32") {
    const netstat = Bun.spawnSync(["netstat", "-ano", "-p", "tcp"], {
      cwd: projectRoot,
      stdout: "pipe",
      stderr: "pipe",
    });
    if (netstat.exitCode !== 0) {
      throw new Error("无法检查 Windows 开发端口占用");
    }
    const targetPortNames = new Set(targetPorts.map(String));
    const pids = new Set();
    for (const line of new TextDecoder()
      .decode(netstat.stdout)
      .split(/\r?\n/)) {
      const columns = line.trim().split(/\s+/);
      if (!/LISTENING/i.test(line)) continue;
      if (!targetPortNames.has(columns[1]?.split(":").at(-1))) continue;
      if (/^\d+$/.test(columns.at(-1))) pids.add(columns.at(-1));
    }
    for (const pid of pids) {
      const result = Bun.spawnSync(["taskkill", "/T", "/F", "/PID", pid], {
        cwd: projectRoot,
        stdout: "ignore",
        stderr: "pipe",
      });
      if (result.exitCode !== 0) {
        throw new Error(`无法清理 Windows 开发进程: pid=${pid}`);
      }
    }
    return;
  }

  const pids = new Set(targetPorts.flatMap((port) => listenerPidsUnix(port)));
  for (const pid of pids) {
    Bun.spawnSync(["kill", "-TERM", pid], {
      cwd: projectRoot,
      stdout: "ignore",
      stderr: "ignore",
    });
  }
  if (pids.size > 0) await Bun.sleep(1_000);
  for (const port of targetPorts) {
    for (const pid of listenerPidsUnix(port)) {
      Bun.spawnSync(["kill", "-KILL", pid], {
        cwd: projectRoot,
        stdout: "ignore",
        stderr: "ignore",
      });
    }
  }
}

function launcherLockPid() {
  const lockPath = path.join(boxteamHome, "state", "launcher.lock");
  if (!existsSync(lockPath)) return null;
  const payload = JSON.parse(readFileSync(lockPath, "utf8"));
  return Number.isInteger(payload.pid) && payload.pid > 0 ? payload.pid : null;
}

async function stopPreviousLauncher() {
  const pid = launcherLockPid();
  if (pid === null) return;
  process.stdout.write(`[dev] 正在停止旧 Launcher: pid=${pid}\n`);
  await terminateProcessWithEscalation(pid, {
    gracefulTimeoutMs: 15_000,
    forceTimeoutMs: 5_000,
    pollIntervalMs: 250,
    sleepImpl: Bun.sleep,
  });
}

async function main() {
  for (const [targetPath, label] of [
    [pythonBin, "Python 解释器"],
    [webRoot, "浏览器前端目录"],
    [terminalFrontendRoot, "终端前端目录"],
    [browserFrontendRoot, "浏览器前端目录"],
    [launcherEntry, "BoxTeam Launcher"],
  ]) {
    requirePath(targetPath, label);
  }
  mkdirSync(defaultWorkspaceRoot, { recursive: true });
  if (serviceMode === "all" || serviceMode === "gateway") {
    await stopPreviousLauncher();
  }
  await cleanDevelopmentPorts();

  const runtimeManifest = writeDevelopmentManifest();
  const environment = {
    ...process.env,
    BOXTEAM_HOME: boxteamHome,
    BOXTEAM_RUNTIME_MANIFEST: runtimeManifest,
    BOXTEAM_DEVELOPMENT_RESTART_RUNNER: process.execPath,
    BOXTEAM_DEVELOPMENT_RESTART_SCRIPT: path.join(
      projectRoot,
      "scripts",
      "dev.mjs",
    ),
    BOXTEAM_DEVELOPMENT_RESTART_CWD: projectRoot,
    BOXTEAM_PYTHON_BIN: pythonBin,
    BOXTEAM_NODE_BIN: nodeBin,
    BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: defaultWorkspaceRoot,
    BOXTEAM_DEV_PORT_OFFSET: String(portOffset),
    BOXTEAM_DEV_FRONTEND_PORT: String(ports.frontend),
    BOXTEAM_GATEWAY_PORT: String(ports.gateway),
    BOXTEAM_TERMINAL_FRONTEND_URL: `http://${host}:${ports.terminalFrontend}`,
    BOXTEAM_BROWSER_FRONTEND_URL: `http://${host}:${ports.browserFrontend}`,
    BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT: String(ports.backendDebug),
    ...(detachedReadyFile === null ? {} : { [SERVICE_LOG_CAPTURED_ENV]: "1" }),
  };
  process.stdout.write(
    `[dev] profile=source-development port_offset=${String(portOffset)} ` +
      `frontend=http://${host}:${ports.frontend} ` +
      `gateway=http://${host}:${ports.gateway} ` +
      `boxteam_home=${boxteamHome}\n`,
  );
  installDevelopmentConfiguration(environment);
  const processes = [];
  if (serviceMode === "backend") {
    processes.push(
      spawnProcess(
        pythonBin,
        [
          "-m",
          "uvicorn",
          "app.main:app",
          "--host",
          host,
          "--port",
          String(ports.backend),
          "--reload",
        ],
        projectRoot,
        environment,
      ),
    );
  } else if (serviceMode === "web") {
    processes.push(spawnProcess("bun", ["run", "dev"], webRoot, environment));
  } else {
    if (serviceMode === "all") {
      installNodeDebugFixture();
      processes.push(
        spawnProcess(
          nodeBin,
          [
            "server.js",
            "--host",
            "0.0.0.0",
            "--port",
            String(ports.terminalFrontend),
            "--backend-url",
            "auto",
            "--workspace-root",
            defaultWorkspaceRoot,
            "--asset-root",
            projectRoot,
          ],
          terminalFrontendRoot,
          environment,
        ),
      );
      processes.push(
        spawnProcess(
          nodeBin,
          [
            "server.js",
            "--host",
            "0.0.0.0",
            "--port",
            String(ports.browserFrontend),
            "--backend-url",
            "auto",
            "--workspace-root",
            defaultWorkspaceRoot,
            "--asset-root",
            projectRoot,
          ],
          browserFrontendRoot,
          environment,
        ),
      );
    }
    processes.push(
      spawnProcess(
        nodeBin,
        [
          launcherEntry,
          "start",
          "--runtime-manifest",
          runtimeManifest,
          "--no-open",
        ],
        projectRoot,
        environment,
      ),
    );
  }

  try {
    if (serviceMode === "backend") {
      await waitForHttpOk(
        `http://${host}:${ports.backend}/api/v1/health`,
        "backend",
      );
    } else if (serviceMode === "web") {
      await waitForHttpOk(`http://${host}:${ports.frontend}/health`, "frontend");
    } else {
      await waitForHttpOk(
        `http://${host}:${ports.gateway}/api/gateway/health`,
        "gateway",
      );
      if (serviceMode === "all") {
        await Promise.all([
          waitForHttpOk(
            `http://${host}:${ports.terminalFrontend}/health`,
            "terminal frontend",
          ),
          waitForHttpOk(
            `http://${host}:${ports.browserFrontend}/health`,
            "browser frontend",
          ),
        ]);
        const frontend = spawnProcess("bun", ["run", "dev"], webRoot, environment);
        processes.push(frontend);
        await waitForHttpOk(`http://${host}:${ports.frontend}/health`, "frontend");
      }
    }
    if (detachedReadyFile !== null) {
      writeFileSync(
        detachedReadyFile,
        `${JSON.stringify({ pid: process.pid, ready_at: new Date().toISOString() })}\n`,
        { encoding: "utf8", mode: 0o600 },
      );
    }
  } catch (error) {
    for (const child of processes) {
      try {
        child.kill();
      } catch {
        // 失败进程可能已经退出；其余进程仍必须继续清理。
      }
    }
    await Promise.allSettled(processes.map((child) => child.exited));
    throw error;
  }

  let stopping = false;
  const stopAll = async (exitCode) => {
    if (stopping) return;
    stopping = true;
    for (const child of processes) {
      try {
        child.kill();
      } catch (error) {
        process.stderr.write(
          `[dev] 停止子进程失败: ${
            error instanceof Error ? error.message : String(error)
          }\n`,
        );
      }
    }
    await Promise.allSettled(processes.map((child) => child.exited));
    process.exit(exitCode);
  };
  for (const child of processes) {
    child.exited
      .then((code) => void stopAll(code))
      .catch((error) => {
        process.stderr.write(`${String(error)}\n`);
        void stopAll(1);
      });
  }
  process.on("SIGINT", () => void stopAll(130));
  process.on("SIGTERM", () => void stopAll(143));
  await Promise.race(processes.map((child) => child.exited));
}

if (onlyLaunch) {
  if (restartDelayMs > 0) {
    await Bun.sleep(restartDelayMs);
  }
  await launchDetachedManager();
} else {
  await main();
}
