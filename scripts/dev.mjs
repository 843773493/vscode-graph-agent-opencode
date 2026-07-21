import path from "node:path";
import os from "node:os";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const boxteamHome = path.resolve(
  process.env.BOXTEAM_HOME ?? path.join(os.homedir(), ".boxteams-dev"),
);
const defaultWorkspaceRoot = path.resolve(
  process.env.BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT ??
    path.join(boxteamHome, "boxteam_workspace"),
);
const webRoot = path.join(projectRoot, "src", "web");
const terminalFrontendRoot = path.join(projectRoot, "src", "terminal", "client");
const browserFrontendRoot = path.join(projectRoot, "src", "browser", "client");
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
const onlyLaunch = process.argv.slice(2).includes("--only-launch");
const host = "127.0.0.1";
const ports = {
  frontend: 8011,
  terminalFrontend: 8013,
  gateway: 8014,
  browserFrontend: 8016,
  backendDebug: 8002,
};

function requirePath(targetPath, label) {
  if (!existsSync(targetPath)) {
    throw new Error(`${label}不存在: ${targetPath}`);
  }
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
  const deadline = Date.now() + 45_000;
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
    `${label}在 45 秒内未就绪: ${url}: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
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
  if (process.platform === "win32") {
    const netstat = Bun.spawnSync(["netstat", "-ano", "-p", "tcp"], {
      cwd: projectRoot,
      stdout: "pipe",
      stderr: "pipe",
    });
    if (netstat.exitCode !== 0) {
      throw new Error("无法检查 Windows 开发端口占用");
    }
    const targetPorts = new Set(Object.values(ports).map(String));
    const pids = new Set();
    for (const line of new TextDecoder().decode(netstat.stdout).split(/\r?\n/)) {
      const columns = line.trim().split(/\s+/);
      if (!/LISTENING/i.test(line)) continue;
      if (!targetPorts.has(columns[1]?.split(":").at(-1))) continue;
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

  const pids = new Set(
    Object.values(ports).flatMap((port) => listenerPidsUnix(port)),
  );
  for (const pid of pids) {
    Bun.spawnSync(["kill", "-TERM", pid], {
      cwd: projectRoot,
      stdout: "ignore",
      stderr: "ignore",
    });
  }
  if (pids.size > 0) await Bun.sleep(1_000);
  for (const port of Object.values(ports)) {
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

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    throw error;
  }
}

async function waitForPreviousLauncherExit() {
  const pid = launcherLockPid();
  if (pid === null || !processIsAlive(pid)) return;
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (!processIsAlive(pid)) return;
    await Bun.sleep(250);
  }
  throw new Error(`旧 Launcher 未在端口清理后退出: pid=${pid}`);
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
  await cleanDevelopmentPorts();
  await waitForPreviousLauncherExit();

  const runtimeManifest = writeDevelopmentManifest();
  const environment = {
    ...process.env,
    BOXTEAM_HOME: boxteamHome,
    BOXTEAM_RUNTIME_MANIFEST: runtimeManifest,
    BOXTEAM_PYTHON_BIN: pythonBin,
    BOXTEAM_NODE_BIN: nodeBin,
    BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: defaultWorkspaceRoot,
    BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE:
      process.env.BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE ??
      path.join(os.homedir(), ".ssh", "boxteam_gateway_e2e_known_hosts"),
    BOXTEAM_REMOTE_PAIR_COMMAND:
      process.env.BOXTEAM_REMOTE_PAIR_COMMAND ??
      "cd /opt/boxteam-dev/repository && " +
        "BOXTEAM_HOME=/home/boxteam/.boxteams-dev " +
        "BOXTEAM_GATEWAY_ROOT=/home/boxteam/.boxteams-dev/state/gateway " +
        "/opt/boxteam-dev/repository/.venv/bin/python " +
        "-m app.gateway.federation_pairing",
    BOXTEAM_CONFIG_OVERLAY_PATHS:
      process.env.BOXTEAM_CONFIG_OVERLAY_PATHS ??
      path.join(projectRoot, "configs", "development.overlay.jsonc"),
    BOXTEAM_TERMINAL_FRONTEND_URL: `http://${host}:${ports.terminalFrontend}`,
    BOXTEAM_BROWSER_FRONTEND_URL: `http://${host}:${ports.browserFrontend}`,
    BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT: String(ports.backendDebug),
  };
  const frontend = spawnProcess("bun", ["run", "dev"], webRoot, environment);
  const terminalFrontend = spawnProcess(
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
  );
  const browserFrontend = spawnProcess(
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
  );
  const launcher = spawnProcess(
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
  const processes = [
    frontend,
    terminalFrontend,
    browserFrontend,
    launcher,
  ];

  try {
    await Promise.all([
      waitForHttpOk(
        `http://${host}:${ports.frontend}/health`,
        "frontend",
      ),
      waitForHttpOk(
        `http://${host}:${ports.gateway}/api/gateway/health`,
        "gateway",
      ),
      waitForHttpOk(
        `http://${host}:${ports.terminalFrontend}/health`,
        "terminal frontend",
      ),
      waitForHttpOk(
        `http://${host}:${ports.browserFrontend}/health`,
        "browser frontend",
      ),
    ]);
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

  if (onlyLaunch) {
    for (const child of processes) child.unref();
    return;
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

await main();
