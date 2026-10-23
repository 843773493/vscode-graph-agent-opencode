import { spawn, spawnSync } from "node:child_process";
import { closeSync } from "node:fs";
import { openServiceLogDescriptor } from "./logging/service-log.mjs";

const DETACHED_TERMINATION_TIMEOUT_MS = 5_000;

export function spawnDetachedProcess({
  command,
  args,
  cwd,
  environment,
  logPath,
}) {
  const logDescriptor = openServiceLogDescriptor(logPath);
  let child;
  const failures = [];
  try {
    child = spawn(command, args, {
      cwd,
      env: environment,
      detached: true,
      stdio: ["ignore", logDescriptor, logDescriptor],
      windowsHide: true,
    });
  } catch (error) {
    failures.push(error);
  }
  try {
    closeSync(logDescriptor);
  } catch (error) {
    failures.push(error);
  }
  if (failures.length > 0) {
    // 交付前失败只回收本次已创建的进程；没有PID的异步创建失败不发终止请求。
    if (Number.isInteger(child?.pid) && child.pid > 0) {
      try {
        terminateDetachedProcess(child.pid);
      } catch (error) {
        failures.push(error);
      }
    }
    if (failures.length === 1) throw failures[0];
    throw new AggregateError(
      failures,
      `detached进程创建或交付失败，资源释放也失败: ${logPath}`,
      { cause: failures[0] },
    );
  }
  return child;
}

export function terminateDetachedProcess(pid) {
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new TypeError(`detached process pid 无效: ${String(pid)}`);
  }
  // TODO: 在 Windows 开发目标上补充 detached process tree 的真实生命周期验证。
  if (process.platform === "win32") {
    const result = spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)], {
      encoding: "utf8",
      stdio: ["ignore", "ignore", "pipe"],
      windowsHide: true,
      timeout: DETACHED_TERMINATION_TIMEOUT_MS,
    });
    if (result.error) {
      throw new Error(`无法执行taskkill: pid=${pid}, error=${result.error.message}`, {
        cause: result.error,
      });
    }
    if (result.status !== 0 && processIsAlive(pid, process.kill)) {
      throw new Error(
        `taskkill失败且目标仍存在: pid=${pid}, exit=${result.status}, signal=${result.signal ?? "none"}, stderr=${result.stderr.trim()}`,
      );
    }
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
  monotonicNowImpl,
}) {
  const deadlineMonotonicMs = monotonicNowImpl() + timeoutMs;
  while (processIsAlive(pid, killImpl)) {
    const remainingMs = deadlineMonotonicMs - monotonicNowImpl();
    if (remainingMs <= 0) return false;
    // Bun.sleep会截断小数毫秒，向上取整避免期限尾部空转。
    await sleepImpl(Math.min(pollIntervalMs, Math.ceil(remainingMs)));
  }
  return true;
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
    // nowImpl注入单调毫秒时钟，不表示Unix时间。
    nowImpl: monotonicNowImpl = performance.now.bind(performance),
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
      monotonicNowImpl,
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
      monotonicNowImpl,
    })
  ) {
    return;
  }
  throw new Error(`进程在 SIGKILL 后仍未退出: pid=${pid}`);
}
