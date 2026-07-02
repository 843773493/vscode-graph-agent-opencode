import path from "node:path";

const scriptsDir = import.meta.dir;
const workspaceRoot = path.resolve(scriptsDir, "..");
const webRoot = path.resolve(workspaceRoot, "src", "web");
const isWindows = process.platform === "win32";
// TODO: 当前仓库保留 Windows 版 tools/bun.exe，Linux/macOS 先复用启动脚本的 Bun。
const bunBin = isWindows
  ? path.resolve(workspaceRoot, "tools", "bun.exe")
  : (process.env.BUN_BIN ?? process.execPath);
// TODO: 兼容 uv 在 Windows 与 Linux/macOS 下创建的虚拟环境脚本目录。
const pythonBin = isWindows
  ? path.resolve(workspaceRoot, ".venv", "Scripts", "python.exe")
  : path.resolve(workspaceRoot, ".venv", "bin", "python");
const port = "8010";
const frontendPort = "8011";
const host = "127.0.0.1";
const backendDebugPort = "8002";
const frontendHealthUrl = `http://${host}:${frontendPort}/health`;
const backendHealthUrl = `http://${host}:${port}/api/v1/health`;
const onlyLaunch = process.argv.slice(2).some((arg) => arg === "--only-launch");

function spawnDetached(command, args, cwd) {
  return Bun.spawn([command, ...args], {
    cwd,
    env: { ...process.env },
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

  for (const targetPort of [port, frontendPort, backendDebugPort]) {
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

async function main() {
  await (isWindows ? killWindowsPort() : killUnixPort());

  const frontend = spawnDetached(bunBin, ["run", "dev"], webRoot);
  const backend = spawnDetached(
    pythonBin,
    [
      "-m",
      "debugpy",
      "--listen",
      `${host}:${backendDebugPort}`,
      "-m",
      "uvicorn",
      "app.main:app",
      "--host",
      host,
      "--port",
      port,
    ],
    workspaceRoot,
  );

  await Promise.all([
    waitForHttpOk(frontendHealthUrl, "frontend"),
    waitForHttpOk(backendHealthUrl, "backend"),
  ]);

  if (onlyLaunch) {
    frontend.unref();
    backend.unref();
    return;
  }

  const stopBoth = async (exitCode) => {
    for (const proc of [frontend, backend]) {
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

  process.on("SIGINT", () => void stopBoth(130));
  process.on("SIGTERM", () => void stopBoth(143));

  await Promise.race([frontend.exited, backend.exited]);
}

await main();
