import path from "node:path";
import {
  existsSync,
  copyFileSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { once } from "node:events";
import { loadRuntimeLogContract } from "../../src/shared/serverRuntimeLogging.mjs";

import {
  spawnDetachedProcess,
  terminateDetachedProcess,
  terminateProcessWithEscalation,
} from "../../packages/launcher/src/detached-process.mjs";
import {
  resolveServiceLogPath,
  SERVICE_LOG_CAPTURED_ENV,
} from "../../packages/launcher/src/logging/service-log.mjs";
import { requestGatewayHandoff } from "../../packages/launcher/src/gateway/supervisor.mjs";
import { BOXTEAM_VERSION } from "../../packaging/runtime/versions.mjs";
import {
  remainingOccupiedPorts,
  resolveDevelopmentLayout,
  resolveBackendWorkspaceId,
} from "./dev-environment.mjs";

import { waitForDetachedReady, waitForHttpOk } from "./dev-readiness.mjs";

const developmentLayout = resolveDevelopmentLayout();
const {
  projectRoot,
  boxteamHome,
  defaultWorkspaceRoot,
  portOffset,
  ports,
} = developmentLayout;
const logContract = loadRuntimeLogContract(projectRoot);
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
// 默认只绑定本机回环；隔离的容器或远程目标需要跨机访问时才显式放开。
const listenHost = process.env.BOXTEAM_DEV_LISTEN_HOST?.trim() || host;
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
    "launch",
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
        version: BOXTEAM_VERSION,
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

async function launchDetachedManager() {
  const runtimeRoot = path.join(projectRoot, "out", "development-runtime");
  mkdirSync(runtimeRoot, { recursive: true });
  const readyFile = path.join(runtimeRoot, "detached-ready.json");
  const logPath = resolveServiceLogPath(boxteamHome, process.env, logContract);
  rmSync(readyFile, { force: true });

  const child = spawnDetachedProcess({
    command: process.execPath,
    args: [path.join(projectRoot, "scripts", "launch", "dev.mjs")],
    cwd: projectRoot,
    environment: {
      ...process.env,
      BOXTEAM_PROJECT_ROOT: projectRoot,
      BOXTEAM_DEV_READY_FILE: readyFile,
    },
    logPath,
    contract: logContract,
  });
  if (!Number.isInteger(child.pid)) {
    throw new Error(`无法取得 detached dev manager pid，日志: ${logPath}`);
  }

  const spawnError = once(child, "error").then(([error]) => {
    throw error;
  });
  try {
    await Promise.race([
      waitForDetachedReady({ child, readyFile, logPath }),
      spawnError,
    ]);
  } catch (error) {
    try {
      terminateDetachedProcess(child.pid);
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        "detached dev manager 启动失败，终止请求也失败",
        { cause: error },
      );
    }
    throw error;
  }
  child.unref();
  process.stdout.write(
    `[dev] detached services ready: pid=${child.pid}, log=${logPath}\n`,
  );
}

function readUnixListenerPidsByPort(targetPorts) {
  // 只清理本地监听者；连接这些端口的客户端不属于开发服务。
  const command = ["lsof", "-nP", "-a", `-iTCP:${targetPorts.join(",")}`, "-sTCP:LISTEN", "-Fpn"];
  const lsof = Bun.spawnSync(command, {
    cwd: projectRoot,
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdout = new TextDecoder().decode(lsof.stdout).trim();
  const stderr = new TextDecoder().decode(lsof.stderr).trim();
  const signalCode = lsof.signalCode ?? null;
  const pidsByPort = new Map(targetPorts.map((port) => [port, []]));
  if (lsof.exitCode === 1 && signalCode === null && stdout === "" && stderr === "") return pidsByPort;
  if (lsof.exitCode !== 0 || signalCode !== null) {
    throw new Error(
      `检查 Unix 开发端口失败: ${command.join(" ")} exit=${String(lsof.exitCode)} ` +
      `signal=${signalCode ?? "none"}${stderr ? `\n${stderr}` : ""}`,
    );
  }
  let currentPid;
  let listenerCount = 0;
  for (const field of stdout.split(/\r?\n/)) {
    if (field.startsWith("p") && /^[1-9]\d*$/.test(field.slice(1))) {
      currentPid = field.slice(1);
    } else if (field.startsWith("n") && currentPid !== undefined) {
      const port = Number(field.slice(field.lastIndexOf(":") + 1));
      if (!pidsByPort.has(port)) {
        throw new Error(`Unix 开发端口查询返回无效监听地址: ${command.join(" ")} field=${JSON.stringify(field)}`);
      }
      const pids = pidsByPort.get(port);
      if (!pids.includes(currentPid)) pids.push(currentPid);
      listenerCount++;
    } else {
      throw new Error(`Unix 开发端口查询返回无效监听记录: ${command.join(" ")} field=${JSON.stringify(field)}`);
    }
  }
  if (listenerCount === 0) {
    throw new Error(`Unix 开发端口查询返回无效监听记录: ${command.join(" ")} stdout=${JSON.stringify(stdout)}`);
  }
  return pidsByPort;
}

function readWindowsListenerPidsByPort(targetPorts) {
  const command = ["netstat", "-ano", "-p", "tcp"];
  const netstat = Bun.spawnSync(command, {
    cwd: projectRoot,
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdout = new TextDecoder().decode(netstat.stdout);
  const stderr = new TextDecoder().decode(netstat.stderr).trim();
  const signalCode = netstat.signalCode ?? null;
  if (netstat.exitCode !== 0 || signalCode !== null) {
    throw new Error(
      `检查 Windows 开发端口失败: ${command.join(" ")} exit=${String(netstat.exitCode)} ` +
      `signal=${signalCode ?? "none"}${stderr ? `\n${stderr}` : ""}`,
    );
  }
  const pidsByPort = new Map(targetPorts.map((port) => [port, []]));
  for (const line of stdout.split(/\r?\n/)) {
    const columns = line.trim().split(/\s+/);
    if (columns[0]?.toUpperCase() !== "TCP" || columns[3]?.toUpperCase() !== "LISTENING") continue;
    const port = Number(columns[1]?.split(":").at(-1));
    if (!pidsByPort.has(port)) continue;
    const pid = columns.at(-1);
    if (!/^[1-9]\d*$/.test(pid)) {
      throw new Error(`Windows 开发端口查询返回无效 PID: port=${port} line=${JSON.stringify(line)}`);
    }
    pidsByPort.get(port).push(pid);
  }
  return pidsByPort;
}

function assertDevelopmentPortsFree(targetPorts, listenerPids) {
  const occupied = remainingOccupiedPorts(targetPorts, listenerPids);
  if (occupied.length === 0) return;
  const details = occupied
    .map(({ port, pids }) => `${port}(pid=${pids.join(",")})`)
    .join("、");
  throw new Error(
    "开发端口清理后仍被占用，拒绝启动（可能是其它用户或权限不足的进程）: " +
      details +
      "；请手动释放这些端口或改用 BOXTEAM_DEV_PORT_OFFSET",
  );
}

async function cleanDevelopmentPorts() {
  const targetPorts = selectedPorts();
  if (process.platform === "win32") {
    // 一次查询覆盖全部目标端口；清理后的确认必须重新取得快照。
    const listenersBeforeCleanup = readWindowsListenerPidsByPort(targetPorts);
    const pids = new Set([...listenersBeforeCleanup.values()].flat());
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
    const listenersAfterCleanup = readWindowsListenerPidsByPort(targetPorts);
    assertDevelopmentPortsFree(targetPorts, (port) => listenersAfterCleanup.get(port));
    return;
  }

  const signalListener = (pid, signal) => {
    try {
      process.kill(Number(pid), signal);
    } catch (error) {
      // 监听查询和发信号之间进程可能已退出；其它失败必须原样暴露。
      if (error.code !== "ESRCH") throw error;
    }
  };
  const listenersBeforeCleanup = readUnixListenerPidsByPort(targetPorts);
  const pids = new Set([...listenersBeforeCleanup.values()].flat());
  for (const pid of pids) {
    signalListener(pid, "SIGTERM");
  }
  if (pids.size > 0) await Bun.sleep(1_000);
  const listenersAfterTerm = readUnixListenerPidsByPort(targetPorts);
  for (const pid of new Set([...listenersAfterTerm.values()].flat())) {
    signalListener(pid, "SIGKILL");
  }
  const listenersAfterCleanup = readUnixListenerPidsByPort(targetPorts);
  assertDevelopmentPortsFree(targetPorts, (port) => listenersAfterCleanup.get(port));
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
      "launch",
      "dev.mjs",
    ),
    BOXTEAM_DEVELOPMENT_RESTART_CWD: projectRoot,
    BOXTEAM_PYTHON_BIN: pythonBin,
    BOXTEAM_NODE_BIN: nodeBin,
    BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: defaultWorkspaceRoot,
    BOXTEAM_DEV_PORT_OFFSET: String(portOffset),
    BOXTEAM_DEV_FRONTEND_PORT: String(ports.frontend),
    BOXTEAM_DEV_LISTEN_HOST: listenHost,
    BOXTEAM_GATEWAY_PORT: String(ports.gateway),
    BOXTEAM_DEFAULT_BACKEND_PORT: String(ports.backend),
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
  const backendWorkspaceId =
    serviceMode === "backend" || serviceMode === "all"
      ? resolveBackendWorkspaceId({
          environment, projectRoot, workspaceRoot: defaultWorkspaceRoot,
          pythonExecutable: pythonBin,
        })
      : null;
  if (backendWorkspaceId !== null) {
    environment.BOXTEAM_WORKSPACE_ID = backendWorkspaceId;
    environment.BOXTEAM_BACKEND_GENERATION = `development_backend_${process.pid}`;
    environment.BOXTEAM_MOUNTED_WORKSPACES = JSON.stringify([
      {
        workspace_id: backendWorkspaceId,
        workspace_root: defaultWorkspaceRoot,
        terminal_backend_url: null,
        browser_backend_url: null,
        config_candidate_ref: null,
        config_generation: null,
        config_fencing_token: null,
      },
    ]);
  }
  const processes = [];
  const pendingGatewayRestart = Boolean(
    environment.BOXTEAM_CONFIG_CANDIDATE_REF?.trim(),
  );
  let gatewayProcess = null;
  if (serviceMode === "backend") {
    process.stdout.write("[dev] backend 日志: stdout-only（当前入口未托管文件）\n");
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
            listenHost,
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
            listenHost,
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
    gatewayProcess = spawnProcess(
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
    );
    processes.push(gatewayProcess);
  }

  let gatewayReady = serviceMode === "backend" || serviceMode === "web";
  try {
    if (serviceMode === "backend") {
      await waitForHttpOk({
        url: `http://${host}:${ports.backend}/api/v1/health`,
        label: "backend",
        headers: { "X-BoxTeam-Workspace-Id": backendWorkspaceId },
      });
    } else if (serviceMode === "web") {
      await waitForHttpOk({ url: `http://${host}:${ports.frontend}/health`, label: "frontend" });
    } else {
      await waitForHttpOk({
        url: `http://${host}:${ports.gateway}/api/gateway/health`,
        label: "gateway",
        processHandle: gatewayProcess,
      });
      gatewayReady = true;
      if (serviceMode === "all") {
        await Promise.all([
          waitForHttpOk({
            url: `http://${host}:${ports.terminalFrontend}/health`,
            label: "terminal frontend",
          }),
          waitForHttpOk({
            url: `http://${host}:${ports.browserFrontend}/health`,
            label: "browser frontend",
          }),
        ]);
        const frontend = spawnProcess("bun", ["run", "dev"], webRoot, environment);
        processes.push(frontend);
        await waitForHttpOk({ url: `http://${host}:${ports.frontend}/health`, label: "frontend" });
      }
    }
    if (detachedReadyFile !== null) {
      writeFileSync(
        detachedReadyFile,
        `${JSON.stringify({
          pid: process.pid,
          ready_at: new Date().toISOString(),
          project_root: projectRoot,
          boxteam_home: boxteamHome,
        })}\n`,
        { encoding: "utf8", mode: 0o600 },
      );
    }
  } catch (error) {
    if (pendingGatewayRestart && !gatewayReady && gatewayProcess !== null) {
      process.stdout.write(
        `[dev] pending Gateway 启动失败，尝试恢复 active snapshot: ${String(error)}\n`,
      );
      try {
        if (
          gatewayProcess.exitCode === null &&
          gatewayProcess.signalCode === null
        ) {
          gatewayProcess.kill();
        }
        await gatewayProcess.exited;
        const fallbackEnvironment = { ...environment };
        for (const variable of [
          "BOXTEAM_CONFIG_CANDIDATE_REF",
          "BOXTEAM_CONFIG_GENERATION",
          "BOXTEAM_CONFIG_FENCING_TOKEN",
        ]) {
          delete fallbackEnvironment[variable];
        }
        const fallbackGatewayProcess = spawnProcess(
          nodeBin,
          [
            launcherEntry,
            "start",
            "--runtime-manifest",
            runtimeManifest,
            "--no-open",
          ],
          projectRoot,
          fallbackEnvironment,
        );
        processes[processes.indexOf(gatewayProcess)] = fallbackGatewayProcess;
        gatewayProcess = fallbackGatewayProcess;
        await waitForHttpOk({
          url: `http://${host}:${ports.gateway}/api/gateway/health`,
          label: "gateway active fallback",
          processHandle: fallbackGatewayProcess,
        });
        gatewayReady = true;
      } catch (fallbackError) {
        for (const child of processes) {
          try {
            child.kill();
          } catch {
            // 失败进程可能已经退出；其余进程仍必须继续清理。
          }
        }
        await Promise.allSettled(processes.map((child) => child.exited));
        throw new Error(
          `Gateway pending 启动失败，且 active fallback 也失败: ${String(fallbackError)}`,
        );
      }
    } else {
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
  if (process.env.BOXTEAM_CONFIG_CANDIDATE_REF?.trim()) {
    const handoff = await requestGatewayHandoff({
      boxteamHome,
      environment: process.env,
    });
    if (handoff.handled) {
      process.stdout.write(
        `[dev] Gateway pending handoff 已交给现有 supervisor: ` +
          `${JSON.stringify(handoff.data)}\n`,
      );
      process.exitCode = 0;
      process.exit();
    }
  }
  await launchDetachedManager();
} else {
  await main();
}
