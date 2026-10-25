import path from "node:path";
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";

import { resolveDevelopmentLayout } from "./dev-environment.mjs";
import { terminateDetachedProcess } from "../../packages/launcher/src/detached-process.mjs";
import { resolveServiceLogPath } from "../../packages/launcher/src/logging/service-log.mjs";

const MANAGER_START_TIMEOUT_MS = 120_000;
const MANAGER_STOP_GRACE_MS = 15_000;
const MANAGER_STOP_FORCE_MS = 5_000;
const STATUS_GATEWAY_TIMEOUT_MS = 2_000;
const OPERATION_LOCK_TIMEOUT_MS = MANAGER_START_TIMEOUT_MS * 2 + 30_000;
const PROCESS_POLL_INTERVAL_MS = 100;

function createManagerLayout(environment = process.env, cwd = process.cwd()) {
  const layout = resolveDevelopmentLayout({ environment, cwd });
  const runtimeRoot = path.join(layout.projectRoot, "out", "development-runtime");
  return {
    ...layout,
    runtimeRoot,
    readyFile: path.join(runtimeRoot, "detached-ready.json"),
    operationLockFile: path.join(runtimeRoot, "manager-operation.lock"),
    logPath: resolveServiceLogPath(layout.boxteamHome, environment),
    frontendUrl: `http://127.0.0.1:${layout.ports.frontend}`,
    gatewayHealthUrl: `http://127.0.0.1:${layout.ports.gateway}/api/gateway/health`,
  };
}

function readManagerRecord(layout) {
  if (!existsSync(layout.readyFile)) return null;
  let record;
  try {
    record = JSON.parse(readFileSync(layout.readyFile, "utf8"));
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new Error(`development manager ready 文件无法解析: ${layout.readyFile}`, {
      cause: error,
    });
  }
  if (
    record === null ||
    typeof record !== "object" ||
    !Number.isInteger(record.pid) ||
    record.pid <= 0 ||
    typeof record.ready_at !== "string"
  ) {
    throw new Error(`development manager ready 文件字段非法: ${layout.readyFile}`);
  }
  if (
    (record.project_root !== undefined && record.project_root !== layout.projectRoot) ||
    (record.boxteam_home !== undefined && record.boxteam_home !== layout.boxteamHome)
  ) {
    throw new Error(`development manager ready 文件不属于当前项目布局: ${layout.readyFile}`);
  }
  return record;
}

function sameManagerRecord(first, second) {
  return first !== null && second !== null &&
    first.pid === second.pid && first.ready_at === second.ready_at;
}

function isProcessAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    if (error?.code === "EPERM") return true;
    throw error;
  }
}

function isProcessGroupAlive(pid) {
  try {
    process.kill(-pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    if (error?.code === "EPERM") return true;
    throw error;
  }
}

async function observeGateway(url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal });
    await response.body?.cancel();
    return {
      ready: response.ok,
      detail: `HTTP ${response.status} ${response.statusText}`,
    };
  } catch (error) {
    return {
      ready: false,
      detail: controller.signal.aborted
        ? `健康请求超过 ${timeoutMs}ms`
        : error instanceof Error ? error.message : String(error),
    };
  } finally {
    clearTimeout(timer);
    controller.abort();
  }
}

async function waitForGateway(layout, initialRecord, timeoutMs = MANAGER_START_TIMEOUT_MS) {
  const deadline = performance.now() + timeoutMs;
  let lastDetail = "manager 尚未写入 ready 文件";
  while (performance.now() < deadline) {
    if (!isProcessAlive(initialRecord.pid)) {
      throw new Error(`development manager 就绪前退出: pid=${initialRecord.pid}`);
    }
    const currentRecord = readManagerRecord(layout);
    if (!sameManagerRecord(currentRecord, initialRecord)) {
      throw new Error("development manager ready 文件在等待期间发生变化");
    }
    const remainingMs = deadline - performance.now();
    const observation = await observeGateway(
      layout.gatewayHealthUrl,
      Math.max(1, Math.min(2_000, Math.ceil(remainingMs))),
    );
    lastDetail = observation.detail;
    if (observation.ready) {
      if (!isProcessAlive(initialRecord.pid) ||
          !sameManagerRecord(readManagerRecord(layout), initialRecord)) {
        throw new Error("Gateway 健康响应结束时 development manager 已退出或更换");
      }
      return;
    }
    await Bun.sleep(Math.max(0, Math.min(250, Math.ceil(deadline - performance.now()))));
  }
  throw new Error(
    `development Gateway 在 ${timeoutMs}ms 内未就绪: ${lastDetail}; log=${layout.logPath}`,
  );
}

async function acquireOperationLock(lockFile) {
  mkdirSync(path.dirname(lockFile), { recursive: true });
  const token = randomUUID();
  const content = JSON.stringify({ pid: process.pid, token });
  const deadline = performance.now() + OPERATION_LOCK_TIMEOUT_MS;
  while (performance.now() < deadline) {
    let descriptor;
    try {
      descriptor = openSync(lockFile, "wx", 0o600);
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      let owner;
      try {
        owner = JSON.parse(readFileSync(lockFile, "utf8"));
      } catch (readError) {
        if (readError?.code === "ENOENT") continue;
        if (!(readError instanceof SyntaxError)) throw readError;
        let lockStat;
        try {
          lockStat = statSync(lockFile);
        } catch (statError) {
          if (statError?.code === "ENOENT") continue;
          throw statError;
        }
        const ageMs = Date.now() - lockStat.mtimeMs;
        if (ageMs > 1_000) {
          throw new Error(`development manager 操作锁无法解析，请检查并移除陈旧锁: ${lockFile}`, {
            cause: readError,
          });
        }
        await Bun.sleep(PROCESS_POLL_INTERVAL_MS);
        continue;
      }
      if (!Number.isInteger(owner.pid) || owner.pid <= 0 || typeof owner.token !== "string") {
        throw new Error(`development manager 操作锁字段非法: ${lockFile}`);
      }
      if (!isProcessAlive(owner.pid)) {
        throw new Error(
          `development manager 操作锁 owner 已退出，请检查并移除陈旧锁: ${lockFile} pid=${owner.pid}`,
        );
      }
      await Bun.sleep(PROCESS_POLL_INTERVAL_MS);
      continue;
    }
    try {
      writeFileSync(descriptor, content, "utf8");
    } catch (error) {
      closeSync(descriptor);
      rmSync(lockFile, { force: true });
      throw error;
    }
    closeSync(descriptor);
    return content;
  }
  throw new Error(`等待 development manager 操作锁超时: ${lockFile}`);
}

function releaseOperationLock(lockFile, content) {
  let current;
  try {
    current = readFileSync(lockFile, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") {
      throw new Error(`development manager 操作锁在释放前消失: ${lockFile}`, {
        cause: error,
      });
    }
    throw error;
  }
  if (current !== content) {
    throw new Error(`development manager 操作锁 owner 在操作期间发生变化: ${lockFile}`);
  }
  rmSync(lockFile);
}

async function withOperationLock(layout, action) {
  const content = await acquireOperationLock(layout.operationLockFile);
  let result;
  let actionFailure;
  let actionFailed = false;
  try {
    result = await action();
  } catch (error) {
    actionFailure = error;
    actionFailed = true;
  }
  try {
    releaseOperationLock(layout.operationLockFile, content);
  } catch (releaseFailure) {
    if (actionFailed) {
      throw new AggregateError(
        [actionFailure, releaseFailure],
        "development manager 操作失败，操作锁释放也失败",
        { cause: actionFailure },
      );
    }
    throw releaseFailure;
  }
  if (actionFailed) throw actionFailure;
  return result;
}

function removeReadyRecord(layout, expectedRecord) {
  const current = readManagerRecord(layout);
  if (sameManagerRecord(current, expectedRecord)) {
    rmSync(layout.readyFile);
  }
}

function printManagerReady(layout, pid, state) {
  process.stdout.write(
    `[dev-manager] ${state}: pid=${pid} frontend=${layout.frontendUrl} ` +
      `boxteam_home=${layout.boxteamHome}\n`,
  );
}

async function startPortable(layout) {
  await withOperationLock(layout, async () => {
    const current = readManagerRecord(layout);
    if (current !== null && isProcessAlive(current.pid)) {
      await waitForGateway(layout, current);
      printManagerReady(layout, current.pid, "already running");
      return;
    }
    if (current !== null) removeReadyRecord(layout, current);

    const command = [
      process.execPath,
      path.join(layout.projectRoot, "scripts", "launch", "dev.mjs"),
      "--only-launch",
    ];
    const result = Bun.spawnSync(command, {
      cwd: layout.projectRoot,
      env: {
        ...process.env,
        BOXTEAM_PROJECT_ROOT: layout.projectRoot,
        BOXTEAM_HOME: layout.boxteamHome,
      },
      stdout: "pipe",
      stderr: "pipe",
    });
    const stdout = result.stdout.toString();
    const stderr = result.stderr.toString();
    const signalCode = result.signalCode ?? null;
    if (stdout) process.stdout.write(stdout);
    if (stderr) process.stderr.write(stderr);
    if (result.exitCode !== 0 || signalCode !== null) {
      throw new Error(
        `detached development stack 启动失败: exit=${String(result.exitCode)} ` +
          `signal=${signalCode ?? "none"}${stderr ? `\n${stderr}` : ""}`,
      );
    }
    const started = readManagerRecord(layout);
    if (started === null) {
      throw new Error(`development manager 启动成功但缺少 ready 文件: ${layout.readyFile}`);
    }
    await waitForGateway(layout, started);
    printManagerReady(layout, started.pid, "ready");
  });
}

async function waitForProcessGroupExit(pid, timeoutMs) {
  const deadline = performance.now() + timeoutMs;
  while (isProcessGroupAlive(pid)) {
    const remainingMs = deadline - performance.now();
    if (remainingMs <= 0) return false;
    await Bun.sleep(Math.min(PROCESS_POLL_INTERVAL_MS, Math.ceil(remainingMs)));
  }
  return true;
}

async function stopWindowsProcessTree(pid) {
  const command = ["taskkill", "/T", "/PID", String(pid)];
  const result = Bun.spawnSync(command, {
    stdout: "pipe",
    stderr: "pipe",
    windowsHide: true,
  });
  const signalCode = result.signalCode ?? null;
  const stderr = result.stderr.toString().trim();
  let gracefulFailure = null;
  if ((result.exitCode !== 0 || signalCode !== null) && isProcessAlive(pid)) {
    gracefulFailure = new Error(
      `请求 Windows development process tree 退出失败: ${command.join(" ")} ` +
        `exit=${String(result.exitCode)} signal=${signalCode ?? "none"}` +
        `${stderr ? `\n${stderr}` : ""}`,
    );
  }
  const gracefulDeadline = performance.now() + MANAGER_STOP_GRACE_MS;
  while (isProcessAlive(pid) && performance.now() < gracefulDeadline) {
    await Bun.sleep(PROCESS_POLL_INTERVAL_MS);
  }
  if (isProcessAlive(pid)) {
    try {
      terminateDetachedProcess(pid);
    } catch (forceFailure) {
      if (gracefulFailure !== null) {
        throw new AggregateError(
          [gracefulFailure, forceFailure],
          "Windows development process tree 优雅停止和强制停止均失败",
          { cause: gracefulFailure },
        );
      }
      throw forceFailure;
    }
    const forceDeadline = performance.now() + MANAGER_STOP_FORCE_MS;
    while (isProcessAlive(pid) && performance.now() < forceDeadline) {
      await Bun.sleep(PROCESS_POLL_INTERVAL_MS);
    }
  }
  if (isProcessAlive(pid)) {
    const forceFailure = new Error(
      `Windows development process tree 在终止期限后仍运行: pid=${pid}`,
    );
    if (gracefulFailure !== null) {
      throw new AggregateError(
        [gracefulFailure, forceFailure],
        "Windows development process tree 优雅停止失败，强制停止后仍在运行",
        { cause: gracefulFailure },
      );
    }
    throw forceFailure;
  }
}

async function stopPortable(layout) {
  await withOperationLock(layout, async () => {
    const record = readManagerRecord(layout);
    if (record === null) {
      process.stdout.write("[dev-manager] not running\n");
      return;
    }
    if (!isProcessAlive(record.pid)) {
      removeReadyRecord(layout, record);
      process.stdout.write(`[dev-manager] removed stale ready marker: pid=${record.pid}\n`);
      return;
    }

    if (process.platform === "win32") {
      await stopWindowsProcessTree(record.pid);
    } else {
      try {
        process.kill(-record.pid, "SIGTERM");
      } catch (error) {
        if (error?.code !== "ESRCH") throw error;
      }
      if (!(await waitForProcessGroupExit(record.pid, MANAGER_STOP_GRACE_MS))) {
        try {
          process.kill(-record.pid, "SIGKILL");
        } catch (error) {
          if (error?.code !== "ESRCH") throw error;
        }
        if (!(await waitForProcessGroupExit(record.pid, MANAGER_STOP_FORCE_MS))) {
          throw new Error(`development process group 在 SIGKILL 后仍运行: pgid=${record.pid}`);
        }
      }
    }
    removeReadyRecord(layout, record);
    process.stdout.write(`[dev-manager] stopped: pid=${record.pid}\n`);
  });
}

async function statusPortable(layout) {
  const record = readManagerRecord(layout);
  const processAlive = record !== null && isProcessAlive(record.pid);
  const observation = processAlive
    ? await observeGateway(layout.gatewayHealthUrl, STATUS_GATEWAY_TIMEOUT_MS)
    : { ready: false, detail: "manager 未运行" };
  const current = record === null ? null : readManagerRecord(layout);
  const managerReady = processAlive && sameManagerRecord(current, record);
  const gatewayReady = managerReady && observation.ready && isProcessAlive(record.pid);
  process.stdout.write(`${JSON.stringify({
    unit: "detached",
    loadState: managerReady ? "loaded" : "not-found",
    activeState: managerReady ? "active" : "inactive",
    subState: managerReady ? "running" : "dead",
    mainPid: managerReady ? record.pid : 0,
    manager_ready: managerReady,
    gateway_ready: gatewayReady,
    gateway_detail: observation.detail,
    frontend: layout.frontendUrl,
    boxteam_home: layout.boxteamHome,
  })}\n`);
  if (!gatewayReady) process.exitCode = 1;
}

export async function main(args = process.argv.slice(2)) {
  const action = args[0] ?? "start";
  if (!new Set(["start", "stop", "status"]).has(action)) {
    throw new Error(`未知 development manager 操作: ${action}`);
  }
  if (process.platform === "linux") {
    const { main: systemdMain } = await import("./dev-systemd.mjs");
    await systemdMain(args);
    return;
  }
  const layout = createManagerLayout();
  if (action === "start") await startPortable(layout);
  if (action === "stop") await stopPortable(layout);
  if (action === "status") await statusPortable(layout);
}

if (import.meta.main) await main();
