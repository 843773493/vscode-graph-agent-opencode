import path from "node:path";
import { existsSync } from "node:fs";

const workspaceRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const runtimeWorkspaceRoot = process.env.WORKSPACE_ROOT
  ? path.resolve(process.env.WORKSPACE_ROOT)
  : workspaceRoot;
const webRoot = path.resolve(workspaceRoot, "src", "web");
const terminalBackendRoot = path.resolve(workspaceRoot, "src", "terminal", "server");
const terminalFrontendRoot = path.resolve(workspaceRoot, "src", "terminal", "client");
const isWindows = process.platform === "win32";
// TODO: 当前仓库保留 Windows 版 tools/bun.exe，Linux/macOS 先复用启动脚本的 Bun。
const bunBin = isWindows
  ? path.resolve(workspaceRoot, "tools", "bun.exe")
  : (process.env.BUN_BIN ?? process.execPath);
const nodeBin = process.env.NODE_BIN ?? (isWindows ? "node.exe" : "node");
// TODO: 兼容 uv 在 Windows 与 Linux/macOS 下创建的虚拟环境脚本目录。
const pythonBin = isWindows
  ? path.resolve(workspaceRoot, ".venv", "Scripts", "python.exe")
  : path.resolve(workspaceRoot, ".venv", "bin", "python");
const port = "8010";
const frontendPort = "8011";
const terminalBackendPort = "8012";
const terminalFrontendPort = "8013";
const host = "127.0.0.1";
const terminalHost = process.env.BOXTEAM_TERMINAL_LISTEN_HOST ?? "0.0.0.0";
const backendDebugPort = "8002";
const frontendHealthUrl = `http://${host}:${frontendPort}/health`;
const backendHealthUrl = `http://${host}:${port}/api/v1/health`;
const terminalBackendHealthUrl = `http://${host}:${terminalBackendPort}/health`;
const terminalFrontendHealthUrl = `http://${host}:${terminalFrontendPort}/health`;
const onlyLaunch = process.argv.slice(2).some((arg) => arg === "--only-launch");

function requirePath(targetPath, label) {
  if (!existsSync(targetPath)) {
    throw new Error(`${label} 不存在: ${targetPath}`);
  }
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

  for (const targetPort of [port, frontendPort, terminalBackendPort, terminalFrontendPort, backendDebugPort]) {
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
  requirePath(webRoot, "浏览器前端目录");
  requirePath(terminalBackendRoot, "终端后端目录");
  requirePath(terminalFrontendRoot, "终端前端目录");
  requirePath(runtimeWorkspaceRoot, "运行工作区目录");

  await (isWindows ? killWindowsPort() : killUnixPort());

  const runtimeEnv = {
    WORKSPACE_ROOT: runtimeWorkspaceRoot,
    BOXTEAM_PROJECT_ROOT: workspaceRoot,
    BOXTEAM_TERMINAL_WORKSPACE_ROOT: runtimeWorkspaceRoot,
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
    runtimeEnv,
  );

  await Promise.all([
    waitForHttpOk(frontendHealthUrl, "frontend"),
    waitForHttpOk(backendHealthUrl, "backend"),
    waitForHttpOk(terminalBackendHealthUrl, "terminal backend"),
    waitForHttpOk(terminalFrontendHealthUrl, "terminal frontend"),
  ]);

  if (onlyLaunch) {
    frontend.unref();
    backend.unref();
    terminalBackend.unref();
    terminalFrontend.unref();
    return;
  }

  const stopBoth = async (exitCode) => {
    for (const proc of [frontend, backend, terminalBackend, terminalFrontend]) {
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

  terminalFrontend.exited
    .then((code) => stopBoth(code))
    .catch((error) => {
      console.error(error);
      void stopBoth(1);
    });

  process.on("SIGINT", () => void stopBoth(130));
  process.on("SIGTERM", () => void stopBoth(143));

  await Promise.race([frontend.exited, backend.exited]);
}

await main();
