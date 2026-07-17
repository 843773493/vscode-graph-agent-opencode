import path from "node:path";
import os from "node:os";
import { existsSync, mkdirSync } from "node:fs";

const workspaceRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const boxteamHome = path.resolve(
  process.env.BOXTEAM_HOME ?? path.join(os.homedir(), ".boxteams"),
);
const defaultUserWorkspaceRoot = path.resolve(
  process.env.BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT ??
    path.join(boxteamHome, "boxteam_workspace"),
);
const runtimeWorkspaceRoot = process.env.WORKSPACE_ROOT
  ? path.resolve(process.env.WORKSPACE_ROOT)
  : defaultUserWorkspaceRoot;
const webRoot = path.resolve(workspaceRoot, "src", "web");
const terminalBackendRoot = path.resolve(workspaceRoot, "src", "terminal", "server");
const terminalFrontendRoot = path.resolve(workspaceRoot, "src", "terminal", "client");
const browserBackendRoot = path.resolve(workspaceRoot, "src", "browser", "server");
const browserFrontendRoot = path.resolve(workspaceRoot, "src", "browser", "client");
const isWindows = process.platform === "win32";
// TODO: 当前仓库保留 Windows 版 tools/bun.exe，Linux/macOS 先复用启动脚本的 Bun。
const bunBin = isWindows
  ? path.resolve(workspaceRoot, "tools", "bun.exe")
  : (process.env.BUN_BIN ?? process.execPath);
const nodeBin = process.env.NODE_BIN ?? (isWindows ? "node.exe" : "node");
// TODO: 兼容 uv 在 Windows 与 Linux/macOS 下创建的虚拟环境脚本目录。
const pythonBin = process.env.BOXTEAM_PYTHON_BIN
  ? path.resolve(process.env.BOXTEAM_PYTHON_BIN)
  : isWindows
    ? path.resolve(workspaceRoot, ".venv", "Scripts", "python.exe")
    : path.resolve(workspaceRoot, ".venv", "bin", "python");
const port = "8010";
const frontendPort = "8011";
const terminalBackendPort = "8012";
const terminalFrontendPort = "8013";
const gatewayPort = "8014";
const browserBackendPort = "8015";
const browserFrontendPort = "8016";
const host = "127.0.0.1";
const terminalHost = process.env.BOXTEAM_TERMINAL_LISTEN_HOST ?? "0.0.0.0";
const browserHost = process.env.BOXTEAM_BROWSER_LISTEN_HOST ?? "0.0.0.0";
const backendDebugPort = "8002";
const frontendHealthUrl = `http://${host}:${frontendPort}/health`;
const backendHealthUrl = `http://${host}:${port}/api/v1/health`;
const gatewayHealthUrl = `http://${host}:${gatewayPort}/api/gateway/health`;
const terminalBackendHealthUrl = `http://${host}:${terminalBackendPort}/health`;
const terminalFrontendHealthUrl = `http://${host}:${terminalFrontendPort}/health`;
const browserBackendHealthUrl = `http://${host}:${browserBackendPort}/health`;
const browserFrontendHealthUrl = `http://${host}:${browserFrontendPort}/health`;
const onlyLaunch = process.argv.slice(2).some((arg) => arg === "--only-launch");

function requirePath(targetPath, label) {
  if (!existsSync(targetPath)) {
    throw new Error(`${label} 不存在: ${targetPath}`);
  }
}

function installUserConfiguration() {
  const generatorPath = path.join(workspaceRoot, "configs", "boxteam.py");
  requirePath(generatorPath, "用户配置生成器");
  const result = Bun.spawnSync(
    [pythonBin, "-m", "configs.boxteam", "--project-root", workspaceRoot],
    {
      cwd: workspaceRoot,
      env: process.env,
      stdout: "pipe",
      stderr: "pipe",
    },
  );
  if (result.exitCode !== 0) {
    const stderr = new TextDecoder().decode(result.stderr).trim();
    throw new Error(`安装用户配置失败: ${stderr || `exit ${result.exitCode}`}`);
  }
  const output = new TextDecoder().decode(result.stdout).trim();
  if (output) console.log(`[dev:config] ${output}`);
}

function spawnDetached(command, args, cwd, envOverrides = {}) {
  return Bun.spawn([command, ...args], {
    cwd,
    env: { ...process.env, ...envOverrides },
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
}

async function waitForHttpOk(url, label) {
  const deadline = Date.now() + 30000;
  let lastError = null;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { method: "GET" });
      if (response.ok) {
        console.log(`[dev:web] ${label} ready: ${url}`);
        return;
      }
      lastError = new Error(
        `${label} returned ${response.status} ${response.statusText}`,
      );
    } catch (error) {
      lastError = error;
    }

    await new Promise((resolve) => setTimeout(resolve, 500));
  }

  throw new Error(
    `[dev:web] ${label} health check failed: ${url}${lastError ? `\n原因: ${lastError instanceof Error ? (lastError.stack ?? lastError.message) : String(lastError)}` : ""}`,
  );
}

async function killWindowsPort() {
  // 直接调用 netstat 并解析输出，避免依赖 shell。
  const netstat = Bun.spawnSync(["netstat", "-ano", "-p", "tcp"], {
    cwd: workspaceRoot,
    stdout: "pipe",
    stderr: "pipe",
  });

  if (netstat.exitCode !== 0) {
    return;
  }

  const output = new TextDecoder().decode(netstat.stdout).trim();
  if (!output) return;

  const pids = new Set();
  for (const line of output.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (!/LISTENING/i.test(trimmed)) continue;
    if (
      !trimmed.includes(`:${port}`) &&
      !trimmed.includes(`:${frontendPort}`) &&
      !trimmed.includes(`:${terminalBackendPort}`) &&
      !trimmed.includes(`:${terminalFrontendPort}`) &&
      !trimmed.includes(`:${gatewayPort}`) &&
      !trimmed.includes(`:${browserBackendPort}`) &&
      !trimmed.includes(`:${browserFrontendPort}`) &&
      !trimmed.includes(`:${backendDebugPort}`)
    )
      continue;
    const cols = trimmed.split(/\s+/);
    const pid = cols.at(-1);
    if (pid && /^\d+$/.test(pid)) pids.add(pid);
  }

  for (const pid of pids) {
    Bun.spawnSync(["taskkill", "/F", "/PID", pid], {
      cwd: workspaceRoot,
      stdout: "ignore",
      stderr: "ignore",
    });
  }
}

async function killUnixPort() {
  // 直接调用系统工具探测并清理占用后端、前端和调试端口的进程。
  const ss = Bun.spawnSync(["ss", "-ltnp"], {
    cwd: workspaceRoot,
    stdout: "pipe",
    stderr: "ignore",
  });
  if (ss.exitCode === 0) {
    const out = new TextDecoder().decode(ss.stdout).trim();
    const pids = new Set();
    for (const line of out.split(/\r?\n/)) {
      if (
        !line.includes(`:${port}`) &&
        !line.includes(`:${frontendPort}`) &&
        !line.includes(`:${terminalBackendPort}`) &&
        !line.includes(`:${terminalFrontendPort}`) &&
        !line.includes(`:${gatewayPort}`) &&
        !line.includes(`:${browserBackendPort}`) &&
        !line.includes(`:${browserFrontendPort}`) &&
        !line.includes(`:${backendDebugPort}`)
      )
        continue;
      const match = line.match(/pid=(\d+)/);
      if (match) pids.add(match[1]);
    }
    for (const pid of pids) {
      Bun.spawnSync(["kill", "-9", pid], {
        cwd: workspaceRoot,
        stdout: "ignore",
        stderr: "ignore",
      });
    }
    return;
  }

  for (const targetPort of [
    port,
    frontendPort,
    terminalBackendPort,
    terminalFrontendPort,
    gatewayPort,
    browserBackendPort,
    browserFrontendPort,
    backendDebugPort,
  ]) {
    const lsof = Bun.spawnSync(["lsof", "-ti", `tcp:${targetPort}`], {
      cwd: workspaceRoot,
      stdout: "pipe",
      stderr: "ignore",
    });
    if (lsof.exitCode === 0) {
      const out = new TextDecoder().decode(lsof.stdout).trim();
      if (out) {
        for (const pid of out.split(/\r?\n/)) {
          if (/^\d+$/.test(pid))
            Bun.spawnSync(["kill", "-9", pid], {
              cwd: workspaceRoot,
              stdout: "ignore",
              stderr: "ignore",
            });
        }
      }
    }
  }
}

function isPortListening(targetPort) {
  if (isWindows) {
    const netstat = Bun.spawnSync(["netstat", "-ano", "-p", "tcp"], {
      cwd: workspaceRoot,
      stdout: "pipe",
      stderr: "ignore",
    });
    if (netstat.exitCode !== 0) {
      return false;
    }
    const out = new TextDecoder().decode(netstat.stdout);
    return out.split(/\r?\n/).some((line) => {
      const trimmed = line.trim();
      return /LISTENING/i.test(trimmed) && trimmed.includes(`:${targetPort}`);
    });
  }

  const ss = Bun.spawnSync(["ss", "-ltn"], {
    cwd: workspaceRoot,
    stdout: "pipe",
    stderr: "ignore",
  });
  if (ss.exitCode !== 0) {
    return false;
  }
  const out = new TextDecoder().decode(ss.stdout);
  return out.split(/\r?\n/).some((line) => line.includes(`:${targetPort}`));
}

async function main() {
  requirePath(path.join(workspaceRoot, "package.json"), "项目 package.json");
  requirePath(path.join(workspaceRoot, "pyproject.toml"), "项目 pyproject.toml");
  requirePath(pythonBin, "Python 解释器");
  requirePath(webRoot, "浏览器前端目录");
  requirePath(terminalBackendRoot, "终端后端目录");
  requirePath(terminalFrontendRoot, "终端前端目录");
  requirePath(browserBackendRoot, "浏览器控制后端目录");
  requirePath(browserFrontendRoot, "浏览器控制前端目录");
  mkdirSync(defaultUserWorkspaceRoot, { recursive: true });
  requirePath(runtimeWorkspaceRoot, "运行工作区目录");

  await (isWindows ? killWindowsPort() : killUnixPort());
  installUserConfiguration();

  const runtimeEnv = {
    WORKSPACE_ROOT: runtimeWorkspaceRoot,
    BOXTEAM_PROJECT_ROOT: workspaceRoot,
    BOXTEAM_TERMINAL_WORKSPACE_ROOT: runtimeWorkspaceRoot,
    BOXTEAM_BROWSER_WORKSPACE_ROOT: runtimeWorkspaceRoot,
    BOXTEAM_BROWSER_BACKEND_URL: `http://${host}:${browserBackendPort}`,
    BOXTEAM_BROWSER_FRONTEND_URL: `http://${host}:${browserFrontendPort}`,
    BOXTEAM_GATEWAY_URL: `http://${host}:${gatewayPort}`,
  };
  const defaultBackendEnv = {
    ...runtimeEnv,
    WORKSPACE_ROOT: defaultUserWorkspaceRoot,
  };

  const frontend = spawnDetached(bunBin, ["run", "dev"], webRoot, runtimeEnv);
  const terminalBackend = spawnDetached(
    nodeBin,
    [
      "backend.js",
      "--host",
      terminalHost,
      "--port",
      terminalBackendPort,
      "--workspace-root",
      runtimeWorkspaceRoot,
      "--frontend-url",
      `http://${host}:${terminalFrontendPort}`,
    ],
    terminalBackendRoot,
    runtimeEnv,
  );
  const terminalFrontend = spawnDetached(
    nodeBin,
    [
      "server.js",
      "--host",
      terminalHost,
      "--port",
      terminalFrontendPort,
      "--backend-url",
      "auto",
      "--workspace-root",
      runtimeWorkspaceRoot,
      "--asset-root",
      workspaceRoot,
    ],
    terminalFrontendRoot,
    runtimeEnv,
  );
  const browserBackend = spawnDetached(
    nodeBin,
    [
      "backend.js",
      "--host",
      browserHost,
      "--port",
      browserBackendPort,
      "--workspace-root",
      runtimeWorkspaceRoot,
      "--frontend-url",
      `http://${host}:${browserFrontendPort}`,
    ],
    browserBackendRoot,
    runtimeEnv,
  );
  const browserFrontend = spawnDetached(
    nodeBin,
    [
      "server.js",
      "--host",
      browserHost,
      "--port",
      browserFrontendPort,
      "--backend-url",
      "auto",
      "--workspace-root",
      runtimeWorkspaceRoot,
      "--asset-root",
      workspaceRoot,
    ],
    browserFrontendRoot,
    runtimeEnv,
  );
  const backendArgs = [];
  if (isPortListening(backendDebugPort)) {
    console.warn(
      `[dev:web] backend debug port ${backendDebugPort} is occupied; starting backend without debugpy.`,
    );
  } else {
    backendArgs.push(
      "-m",
      "debugpy",
      "--listen",
      `${host}:${backendDebugPort}`,
    );
  }
  backendArgs.push(
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    host,
    "--port",
    port,
  );
  const backend = spawnDetached(
    pythonBin,
    backendArgs,
    workspaceRoot,
    defaultBackendEnv,
  );
  const gateway = spawnDetached(
    pythonBin,
    [
      "-m",
      "uvicorn",
      "app.gateway.main:app",
      "--host",
      host,
      "--port",
      gatewayPort,
      "--log-level",
      "warning",
    ],
    workspaceRoot,
    {
      ...runtimeEnv,
      BOXTEAM_GATEWAY_ROOT: path.join(boxteamHome, "state", "gateway"),
      BOXTEAM_DEFAULT_BACKEND_URL: `http://${host}:${port}`,
      BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: defaultUserWorkspaceRoot,
      BOXTEAM_BROWSER_BACKEND_URL: `http://${host}:${browserBackendPort}`,
      BOXTEAM_BROWSER_FRONTEND_URL: `http://${host}:${browserFrontendPort}`,
    },
  );

  await Promise.all([
    waitForHttpOk(frontendHealthUrl, "frontend"),
    waitForHttpOk(backendHealthUrl, "backend"),
    waitForHttpOk(gatewayHealthUrl, "gateway"),
    waitForHttpOk(terminalBackendHealthUrl, "terminal backend"),
    waitForHttpOk(terminalFrontendHealthUrl, "terminal frontend"),
    waitForHttpOk(browserBackendHealthUrl, "browser backend"),
    waitForHttpOk(browserFrontendHealthUrl, "browser frontend"),
  ]);

  if (onlyLaunch) {
    frontend.unref();
    backend.unref();
    gateway.unref();
    terminalBackend.unref();
    terminalFrontend.unref();
    browserBackend.unref();
    browserFrontend.unref();
    return;
  }

  const stopBoth = async (exitCode) => {
    for (const proc of [
      frontend,
      backend,
      gateway,
      terminalBackend,
      terminalFrontend,
      browserBackend,
      browserFrontend,
    ]) {
      try {
        proc.kill();
      } catch {
        // 进程可能已经退出，直接忽略。
      }
    }
    process.exit(exitCode);
  };

  frontend.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  backend.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  terminalBackend.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  gateway.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  terminalFrontend.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  browserBackend.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  browserFrontend.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  process.on("SIGINT", () => void stopBoth(130));
  process.on("SIGTERM", () => void stopBoth(143));

  await Promise.race([
    frontend.exited,
    backend.exited,
    gateway.exited,
    terminalBackend.exited,
    terminalFrontend.exited,
    browserBackend.exited,
    browserFrontend.exited,
  ]);
}

await main();
