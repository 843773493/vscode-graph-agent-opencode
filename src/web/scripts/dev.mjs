import path from "node:path";

const scriptsDir = import.meta.dir;
const workspaceRoot = path.resolve(scriptsDir, "..", "..", "..");
const webRoot = path.resolve(scriptsDir, "..");
const bunBin = path.resolve(workspaceRoot, "tools", "bun.exe");
const port = "8000";
const host = "127.0.0.1";
const isWindows = process.platform === "win32";

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
  const netstat = Bun.spawnSync(
    ["cmd", "/c", `netstat -ano -p tcp | findstr :${port}`],
    {
      cwd: workspaceRoot,
      stdout: "pipe",
      stderr: "ignore",
    },
  );

  if (netstat.exitCode !== 0) {
    return;
  }

  const output = new TextDecoder().decode(netstat.stdout).trim();
  if (!output) {
    return;
  }

  const pids = new Set();
  for (const line of output.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || !trimmed.includes(`:${port}`) || !trimmed.includes('LISTENING')) {
      continue;
    }

    const columns = trimmed.split(/\s+/);
    const pid = columns.at(-1);
    if (pid && /^\d+$/.test(pid)) {
      pids.add(pid);
    }
  }

  for (const pid of pids) {
    await runQuiet("cmd", ["/c", "taskkill", "/F", "/PID", pid]);
  }
}

async function killUnixPort() {
  const hasSs =
    Bun.spawnSync(["sh", "-lc", "command -v ss >/dev/null 2>&1"], {
      cwd: workspaceRoot,
      stdout: "ignore",
      stderr: "ignore",
    }).exitCode === 0;

  if (hasSs) {
    const command = `ss -ltnp "sport = :${port}" 2>/dev/null | awk -F'pid=|,' 'NR>1 {print $2}' | sort -u | while IFS= read -r pid; do [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true; done`;
    await runQuiet("sh", ["-lc", command]);
    return;
  }

  const hasLsof =
    Bun.spawnSync(["sh", "-lc", "command -v lsof >/dev/null 2>&1"], {
      cwd: workspaceRoot,
      stdout: "ignore",
      stderr: "ignore",
    }).exitCode === 0;

  if (hasLsof) {
    const command = `lsof -ti tcp:${port} 2>/dev/null | while IFS= read -r pid; do [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true; done`;
    await runQuiet("sh", ["-lc", command]);
  }
}

await (isWindows ? killWindowsPort() : killUnixPort());

const frontend = spawnDetached(
  bunBin,
  ["x", "vite", "--host", "127.0.0.1"],
  webRoot,
);
const backend = spawnDetached(
  "uv",
  ["run", "uvicorn", "app.main:app", "--host", host, "--port", port],
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
