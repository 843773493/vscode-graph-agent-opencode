import path from "node:path";

const scriptsDir = import.meta.dir;
const workspaceRoot = path.resolve(scriptsDir, "..", "..", "..");
const webRoot = path.resolve(scriptsDir, "..");
const bunBin = path.resolve(workspaceRoot, "tools", "bun.exe");
const pythonBin = path.resolve(workspaceRoot, ".venv", "Scripts", "python.exe");
const port = "8000";
const host = "127.0.0.1";
const isWindows = process.platform === "win32";
const frontendInspectPort = "9229";
const backendDebugPort = "5678";

function spawnDetached(command, args, cwd) {
  return Bun.spawn([command, ...args], {
    cwd,
    env: { ...process.env },
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
}

function runQuiet(command, args, cwd = workspaceRoot) {
  const proc = Bun.spawn([command, ...args], {
    cwd,
    stdin: "ignore",
    stdout: "ignore",
    stderr: "ignore",
  });

  return proc.exited.catch(() => 0);
}

async function killWindowsPort() {
  // Run netstat directly (no shell) and parse output to find PIDs
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
    // netstat columns: Proto Local Address Foreign Address State PID
    if (!trimmed.includes(`:${port}`) || !/LISTENING/i.test(trimmed)) continue;
    const cols = trimmed.split(/\s+/);
    const pid = cols.at(-1);
    if (pid && /^\d+$/.test(pid)) pids.add(pid);
  }

  for (const pid of pids) {
    Bun.spawnSync(["taskkill", "/F", "/PID", pid], { cwd: workspaceRoot, stdout: "ignore", stderr: "ignore" });
  }
}

async function killUnixPort() {
  // Try ss, then lsof, then netstat parsing — all invoked directly without a shell
  const ss = Bun.spawnSync(["ss", "-ltnp"], { cwd: workspaceRoot, stdout: "pipe", stderr: "ignore" });
  if (ss.exitCode === 0) {
    const out = new TextDecoder().decode(ss.stdout).trim();
    const pids = new Set();
    for (const line of out.split(/\r?\n/)) {
      if (!line.includes(`:${port}`)) continue;
      // try to extract pid=NUMBER
      const m = line.match(/pid=(\d+)/);
      if (m) pids.add(m[1]);
    }
    for (const pid of pids) {
      Bun.spawnSync(["kill", "-9", pid], { cwd: workspaceRoot, stdout: "ignore", stderr: "ignore" });
    }
    return;
  }

  const lsof = Bun.spawnSync(["lsof", "-ti", `tcp:${port}`], { cwd: workspaceRoot, stdout: "pipe", stderr: "ignore" });
  if (lsof.exitCode === 0) {
    const out = new TextDecoder().decode(lsof.stdout).trim();
    if (out) {
      for (const pid of out.split(/\r?\n/)) {
        if (/^\d+$/.test(pid)) Bun.spawnSync(["kill", "-9", pid], { cwd: workspaceRoot, stdout: "ignore", stderr: "ignore" });
      }
    }
    return;
  }

  // fallback: try netstat -nlp and parse PID column (may require privileges)
  const netstat = Bun.spawnSync(["netstat", "-nlp", "tcp"], { cwd: workspaceRoot, stdout: "pipe", stderr: "ignore" });
  if (netstat.exitCode !== 0) return;
  const out = new TextDecoder().decode(netstat.stdout).trim();
  const pids = new Set();
  for (const line of out.split(/\r?\n/)) {
    if (!line.includes(`:${port}`)) continue;
    const m = line.match(/\b(\d+)\/(?:\S+)/); // PID/Program
    if (m) pids.add(m[1]);
  }
  for (const pid of pids) Bun.spawnSync(["kill", "-9", pid], { cwd: workspaceRoot, stdout: "ignore", stderr: "ignore" });
}

await (isWindows ? killWindowsPort() : killUnixPort());

const frontend = spawnDetached(
  bunBin,
  [`--inspect=${host}:${frontendInspectPort}`, "x", "vite", "--host", host],
  webRoot,
);
const backend = spawnDetached(
  pythonBin,
  ["-m", "debugpy", "--listen", `${host}:${backendDebugPort}`, "-m", "uvicorn", "app.main:app", "--host", host, "--port", port],
  workspaceRoot,
);

const stopBoth = async (exitCode) => {
  const processes = [frontend, backend];
  for (const proc of processes) {
    try {
      proc.kill();
    } catch {
      // 进程可能已经退出，忽略即可
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
