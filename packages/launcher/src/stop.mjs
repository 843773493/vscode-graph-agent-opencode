import {
  processIsAlive,
  readLauncherLock,
  removeLauncherLock,
} from "./instance-lock.mjs";

export const DEFAULT_STOP_TIMEOUT_MS = 15_000;
export const DEFAULT_FORCE_STOP_TIMEOUT_MS = 5_000;
const STOP_POLL_INTERVAL_MS = 100;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function lockHasTargetToken(current, target) {
  return current?.value?.token === target.value?.token;
}

function validateLauncherPid(lock) {
  const pid = lock?.value?.pid;
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new Error(`Launcher 锁中的 pid 无效: ${String(pid)}`);
  }
  return pid;
}

async function waitForLauncherStop({
  boxteamHome,
  targetLock,
  pid,
  timeoutMs,
  readLockImpl,
  removeLockImpl,
  isAliveImpl,
  sleepImpl,
  nowImpl,
}) {
  const deadlineMonotonicMs = nowImpl() + timeoutMs;
  while (true) {
    const currentLock = readLockImpl(boxteamHome);
    if (!isAliveImpl(pid)) {
      if (lockHasTargetToken(currentLock, targetLock)) {
        removeLockImpl(boxteamHome, targetLock.value.token);
      }
      return true;
    }
    if (currentLock !== null && !lockHasTargetToken(currentLock, targetLock)) {
      throw new Error("停止 Launcher 期间锁被其它实例替换");
    }
    const remainingMs = deadlineMonotonicMs - nowImpl();
    if (remainingMs <= 0) return false;
    await sleepImpl(Math.min(STOP_POLL_INTERVAL_MS, remainingMs));
  }
}

export async function stopLauncher({
  boxteamHome,
  readLockImpl = readLauncherLock,
  removeLockImpl = removeLauncherLock,
  isAliveImpl = processIsAlive,
  killImpl = process.kill,
  sleepImpl = delay,
  nowImpl = performance.now.bind(performance),
  timeoutMs = DEFAULT_STOP_TIMEOUT_MS,
  forceTimeoutMs = DEFAULT_FORCE_STOP_TIMEOUT_MS,
} = {}) {
  const targetLock = readLockImpl(boxteamHome);
  if (targetLock === null) return Object.freeze({ status: "not-running" });

  const pid = validateLauncherPid(targetLock);
  if (!isAliveImpl(pid)) {
    removeLockImpl(boxteamHome, targetLock.value.token);
    return Object.freeze({ status: "stale-lock-removed", pid });
  }

  try {
    killImpl(pid, "SIGTERM");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
    removeLockImpl(boxteamHome, targetLock.value.token);
    return Object.freeze({ status: "stopped", pid });
  }

  const stoppedGracefully = await waitForLauncherStop({
    boxteamHome,
    targetLock,
    pid,
    timeoutMs,
    readLockImpl,
    removeLockImpl,
    isAliveImpl,
    sleepImpl,
    nowImpl,
  });
  if (stoppedGracefully) {
    return Object.freeze({ status: "stopped", pid });
  }

  try {
    killImpl(pid, "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
    removeLockImpl(boxteamHome, targetLock.value.token);
    return Object.freeze({ status: "force-stopped", pid });
  }
  const stoppedAfterKill = await waitForLauncherStop({
    boxteamHome,
    targetLock,
    pid,
    timeoutMs: forceTimeoutMs,
    readLockImpl,
    removeLockImpl,
    isAliveImpl,
    sleepImpl,
    nowImpl,
  });
  if (!stoppedAfterKill) {
    throw new Error(`Launcher 在 ${forceTimeoutMs}ms 内未退出: pid=${String(pid)}`);
  }
  return Object.freeze({ status: "force-stopped", pid });
}
