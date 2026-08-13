import { execFile, spawnSync } from "node:child_process";
import { readFile, readdir } from "node:fs/promises";
import { promisify } from "node:util";

const TERMINATION_GRACE_MS = 1500;
const execFileAsync = promisify(execFile);

export function parseLinuxProcStat(raw) {
  const endOfCommand = raw.lastIndexOf(")");
  if (endOfCommand === -1) {
    throw new Error(`无法解析 /proc stat: ${raw}`);
  }
  const pid = Number(raw.slice(0, raw.indexOf(" ")));
  const fields = raw.slice(endOfCommand + 2).trim().split(/\s+/);
  return {
    pid,
    ppid: Number(fields[1]),
    processGroupId: Number(fields[2]),
    processSessionId: Number(fields[3]),
    processStartTime: Number(fields[19]),
  };
}

export function parsePosixPsStat(raw, { numericSession = true } = {}) {
  const fields = raw.trim().split(/\s+/);
  if (fields.length < 9) {
    return null;
  }
  return {
    pid: Number(fields[0]),
    ppid: Number(fields[1]),
    processGroupId: Number(fields[2]),
    processSessionId: numericSession ? Number(fields[3]) : fields[3],
    processStartTime: fields.slice(4).join(" "),
  };
}

export function parsePosixSessionProcesses(raw, processSessionId) {
  return raw
    .trim()
    .split("\n")
    .map((line) => line.trim().split(/\s+/))
    .filter(([rawPid, rawSessionId]) => (
      Number.isInteger(Number(rawPid))
      && Number(rawPid) !== process.pid
      && (
        typeof processSessionId === "number"
          ? Number(rawSessionId) === processSessionId
          : rawSessionId === processSessionId
      )
    ))
    .map(([rawPid]) => Number(rawPid))
    .sort((left, right) => right - left);
}

function isValidSessionId(processSessionId) {
  return (
    (Number.isInteger(processSessionId) && processSessionId > 0)
    || (
      typeof processSessionId === "string"
      && processSessionId.length > 0
    )
  );
}

async function readPosixProcessStat(pid) {
  // TODO: 平台兼容：Darwin 的 ps 使用 sess 指针标识 session，其他 POSIX 使用 sid。
  const sessionField = process.platform === "darwin" ? "sess=" : "sid=";
  try {
    const { stdout } = await execFileAsync(
      "ps",
      [
        "-o", "pid=",
        "-o", "ppid=",
        "-o", "pgid=",
        "-o", sessionField,
        "-o", "lstart=",
        "-p", String(pid),
      ],
      { encoding: "utf8" },
    );
    return parsePosixPsStat(stdout, {
      numericSession: process.platform !== "darwin",
    });
  } catch (error) {
    if (!processExists(pid)) {
      return null;
    }
    throw error;
  }
}

export async function readProcessStat(pid) {
  if (!Number.isInteger(pid) || pid <= 0) {
    return null;
  }
  if (process.platform === "win32") {
    return null;
  }
  if (process.platform !== "linux") {
    return await readPosixProcessStat(pid);
  }
  try {
    return parseLinuxProcStat(await readFile(`/proc/${pid}/stat`, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT" || error?.code === "ESRCH") {
      return null;
    }
    throw error;
  }
}

export function processExists(pid) {
  if (!Number.isInteger(pid) || pid <= 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") {
      return false;
    }
    if (error?.code === "EPERM") {
      return true;
    }
    throw error;
  }
}

function signalProcess(pid, signal) {
  try {
    process.kill(pid, signal);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") {
      return false;
    }
    throw error;
  }
}

function taskkill(pid) {
  const result = spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)], {
    stdout: "ignore",
    stderr: "ignore",
  });
  return result.status === 0 || !processExists(pid);
}

async function currentProcessSessionId() {
  return (await readProcessStat(process.pid))?.processSessionId ?? null;
}

async function processIdsInSession(processSessionId) {
  if (!isValidSessionId(processSessionId)) {
    return [];
  }
  const currentSessionId = await currentProcessSessionId();
  if (currentSessionId === processSessionId) {
    return [];
  }
  if (process.platform !== "linux") {
    // TODO: 平台兼容：保持与单进程元数据查询相同的 Darwin sess / POSIX sid 语义。
    const sessionField = process.platform === "darwin" ? "sess=" : "sid=";
    const { stdout } = await execFileAsync(
      "ps",
      ["-ax", "-o", "pid=", "-o", sessionField],
      { encoding: "utf8" },
    );
    return parsePosixSessionProcesses(stdout, processSessionId);
  }
  const entries = await readdir("/proc", { withFileTypes: true });
  const pids = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) {
      continue;
    }
    const pid = Number(entry.name);
    if (pid === process.pid) {
      continue;
    }
    const stat = await readProcessStat(pid);
    if (stat?.processSessionId === processSessionId) {
      pids.push(pid);
    }
  }
  return pids.sort((left, right) => right - left);
}

async function waitForProcessesExit(pids, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pids.every((pid) => !processExists(pid))) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return pids.every((pid) => !processExists(pid));
}

async function terminalProcessIds({
  pid,
  processSessionId = null,
  processStartTime = null,
}) {
  const stat = await readProcessStat(pid);
  if (processStartTime && stat?.processStartTime && stat.processStartTime !== processStartTime) {
    return [];
  }
  const effectiveSessionId = processSessionId || stat?.processSessionId || null;
  const sessionPids = await processIdsInSession(effectiveSessionId);
  if (sessionPids.length > 0) {
    return sessionPids;
  }
  return processExists(pid) ? [pid] : [];
}

export async function terminateTerminalProcessTree({
  pid,
  processSessionId = null,
  processStartTime = null,
}) {
  if (process.platform === "win32") {
    return taskkill(pid) ? "terminated" : "missing";
  }

  const termTargets = await terminalProcessIds({ pid, processSessionId, processStartTime });
  if (termTargets.length === 0) {
    return "missing";
  }

  for (const targetPid of termTargets) {
    signalProcess(targetPid, "SIGTERM");
  }
  if (await waitForProcessesExit(termTargets, TERMINATION_GRACE_MS)) {
    return "terminated";
  }

  const killTargets = await terminalProcessIds({ pid, processSessionId, processStartTime });
  for (const targetPid of killTargets) {
    signalProcess(targetPid, "SIGKILL");
  }
  await waitForProcessesExit(killTargets, TERMINATION_GRACE_MS);
  return killTargets.some((targetPid) => processExists(targetPid))
    ? "still_running"
    : "force_killed";
}
