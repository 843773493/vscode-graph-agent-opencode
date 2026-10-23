import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";

const POSIX_PROCESS_GROUP_EXIT_TIMEOUT_MS = 5_000;
const POSIX_TERMINATION_POLL_MS = 100;
const WINDOWS_TASKKILL_TIMEOUT_MS = 10_000;
const monotonicNow = performance.now.bind(performance);

function processGroupExists(processGroupId) {
  try {
    process.kill(-processGroupId, 0);
    return true;
  } catch (error) {
    if (error.code === "ESRCH") {
      return false;
    }
    throw error;
  }
}

function signalProcessGroupSafely(processGroupId, name, signal, signalProcessGroup) {
  try {
    signalProcessGroup(processGroupId, signal);
    return true;
  } catch (error) {
    if (error.code === "ESRCH") {
      return false;
    }
    throw new Error(`无法向 ${name} 的进程组发送 ${signal}: ${error.message}`, {
      cause: error,
    });
  }
}

async function waitForProcessGroupExit(
  processGroupId,
  timeoutMs,
  { isProcessGroupAlive, sleep, now, pollIntervalMs },
) {
  const deadline = now() + timeoutMs;
  while (isProcessGroupAlive(processGroupId)) {
    const remainingMs = deadline - now();
    if (remainingMs <= 0) {
      return false;
    }
    await sleep(Math.min(pollIntervalMs, remainingMs));
  }
  return true;
}

export async function terminateChildProcessTree(
  { child, name },
  {
    platform = process.platform,
    spawnProcess = spawn,
    signalProcessGroup = (processGroupId, signal) => process.kill(-processGroupId, signal),
    isProcessGroupAlive = processGroupExists,
    sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    now = monotonicNow,
    processGroupExitTimeoutMs = POSIX_PROCESS_GROUP_EXIT_TIMEOUT_MS,
    pollIntervalMs = POSIX_TERMINATION_POLL_MS,
  } = {},
) {
  if (!child.pid) {
    return;
  }

  if (platform !== "win32") {
    const processGroupId = child.pid;
    if (!signalProcessGroupSafely(processGroupId, name, "SIGTERM", signalProcessGroup)) {
      return;
    }
    if (await waitForProcessGroupExit(processGroupId, processGroupExitTimeoutMs, {
      isProcessGroupAlive,
      sleep,
      now,
      pollIntervalMs,
    })) {
      return;
    }
    if (!signalProcessGroupSafely(processGroupId, name, "SIGKILL", signalProcessGroup)) {
      return;
    }
    if (!await waitForProcessGroupExit(processGroupId, processGroupExitTimeoutMs, {
      isProcessGroupAlive,
      sleep,
      now,
      pollIntervalMs,
    })) {
      throw new Error(
        `无法回收 ${name} 的进程组: SIGKILL 后 ${processGroupExitTimeoutMs}ms 仍未退出，pid=${child.pid}`,
      );
    }
    return;
  }

  const terminator = spawnProcess("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
    stdio: "ignore",
  });
  await new Promise((resolve, reject) => {
    let settled = false;
    let taskkillTimeout;
    const finish = (callback, value) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(taskkillTimeout);
      callback(value);
    };
    const fail = (details, cause) => {
      finish(reject, new Error(`无法回收 ${name} 的进程树: ${details}`, { cause }));
    };

    terminator.once("error", (error) => fail(error.message, error));
    terminator.once("exit", (code, signal) => {
      if (code !== 0) {
        fail(`taskkill code=${code ?? "null"} signal=${signal ?? "null"}`);
        return;
      }
      finish(resolve);
    });
    taskkillTimeout = setTimeout(() => {
      const timeoutError = new Error(`taskkill.exe 等待超过 ${WINDOWS_TASKKILL_TIMEOUT_MS}ms`);
      terminator.kill();
      finish(
        reject,
        new Error(`无法确认 ${name} 的 Windows 进程树已回收: ${timeoutError.message}`, {
          cause: timeoutError,
        }),
      );
    }, WINDOWS_TASKKILL_TIMEOUT_MS);
  });
}
