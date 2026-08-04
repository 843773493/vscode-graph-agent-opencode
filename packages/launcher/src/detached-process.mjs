import { spawn, spawnSync } from "node:child_process";
import { chmodSync, closeSync, mkdirSync, openSync } from "node:fs";
import path from "node:path";

export function spawnDetachedProcess({
  command,
  args,
  cwd,
  environment,
  logPath,
}) {
  mkdirSync(path.dirname(logPath), { recursive: true, mode: 0o700 });
  const logFd = openSync(logPath, "a", 0o600);
  try {
    chmodSync(logPath, 0o600);
    return spawn(command, args, {
      cwd,
      env: environment,
      detached: true,
      stdio: ["ignore", logFd, logFd],
      windowsHide: true,
    });
  } finally {
    closeSync(logFd);
  }
}

export function terminateDetachedProcess(pid) {
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new TypeError(`detached process pid 无效: ${String(pid)}`);
  }
  // TODO: 在 Windows 开发目标上补充 detached process tree 的真实生命周期验证。
  if (process.platform === "win32") {
    const result = spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)], {
      stdio: "ignore",
      windowsHide: true,
    });
    if (result.status !== 0 && result.error) throw result.error;
    return;
  }
  try {
    process.kill(-pid, "SIGTERM");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
}

function processIsAlive(pid, killImpl) {
  try {
    killImpl(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    throw error;
  }
}

async function waitForProcessExit({
  pid,
  timeoutMs,
  pollIntervalMs,
  killImpl,
  sleepImpl,
  nowImpl,
}) {
  const deadline = nowImpl() + timeoutMs;
  while (nowImpl() < deadline) {
    if (!processIsAlive(pid, killImpl)) return true;
    await sleepImpl(Math.min(pollIntervalMs, deadline - nowImpl()));
  }
  return !processIsAlive(pid, killImpl);
}

export async function terminateProcessWithEscalation(
  pid,
  {
    gracefulTimeoutMs = 15_000,
    forceTimeoutMs = 5_000,
    pollIntervalMs = 100,
    killImpl = process.kill,
    sleepImpl = (delayMs) =>
      new Promise((resolve) => setTimeout(resolve, delayMs)),
    nowImpl = Date.now,
  } = {},
) {
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new TypeError(`process pid 无效: ${String(pid)}`);
  }
  if (!processIsAlive(pid, killImpl)) return;

  try {
    killImpl(pid, "SIGTERM");
  } catch (error) {
    if (error?.code === "ESRCH") return;
    throw error;
  }
  if (
    await waitForProcessExit({
      pid,
      timeoutMs: gracefulTimeoutMs,
      pollIntervalMs,
      killImpl,
      sleepImpl,
      nowImpl,
    })
  ) {
    return;
  }

  try {
    killImpl(pid, "SIGKILL");
  } catch (error) {
    if (error?.code === "ESRCH") return;
    throw error;
  }
  if (
    await waitForProcessExit({
      pid,
      timeoutMs: forceTimeoutMs,
      pollIntervalMs,
      killImpl,
      sleepImpl,
      nowImpl,
    })
  ) {
    return;
  }
  throw new Error(`进程在 SIGKILL 后仍未退出: pid=${pid}`);
}
