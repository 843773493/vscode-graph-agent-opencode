import { spawnSync } from "node:child_process";
import { readFile, readdir } from "node:fs/promises";

const TERMINATION_GRACE_MS = 1500;

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

export async function readLinuxProcStat(pid) {
  if (process.platform !== "linux" || !Number.isInteger(pid) || pid <= 0) {
    return null;
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
  return (await readLinuxProcStat(process.pid))?.processSessionId ?? null;
}

async function processIdsInSession(processSessionId) {
  if (process.platform !== "linux" || !Number.isInteger(processSessionId) || processSessionId <= 0) {
    return [];
  }
  const currentSessionId = await currentProcessSessionId();
  if (currentSessionId === processSessionId) {
    return [];
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
    const stat = await readLinuxProcStat(pid);
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
  const stat = await readLinuxProcStat(pid);
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
