export interface ProcessStat {
  pid: number;
  state?: string;
  ppid: number;
  processGroupId: number;
  processSessionId: number | string;
  processStartTime: number | string;
}
import { execFile } from "node:child_process";
import { readFile, readdir } from "node:fs/promises";
import { promisify } from "node:util";
import { boundedFailureText, failureMessage, formatFailureDiagnostic } from "./failures.js";
import { createRuntimeLogger } from "./logging.js";

type ProcessOperationFailure = Error & { code?: string | number; signal?: string | null; killed?: boolean };
interface ProcessIdentity { pid: number; processSessionId?: number | string | null; processStartTime?: number | string | null }

const TERMINATION_GRACE_MS = 1500;
const PROCESS_SCAN_BATCH_SIZE = 64;
const execFileAsync = promisify(execFile);
const logger = createRuntimeLogger("process", "process.lifecycle");

function processOperationError(stage: string, primary: ProcessOperationFailure, { pid = null, processSessionId = null, signal = primary.signal ?? "none" }: { pid?: number | null; processSessionId?: number | string | null; signal?: string | null } = {}, cause: unknown = primary) {
  const code = primary.code ?? "none";
  const message = boundedFailureText(`进程操作失败: stage=${stage}, pid=${pid}, process_session_id=${processSessionId}, type=${primary.name}, code=${code}, signal=${signal}, reason=${failureMessage(primary)}`);
  try {
    logger.error(`[process] ${message}`, formatFailureDiagnostic(cause, { maxBytes: 65536 - new TextEncoder().encode(message).byteLength - 16 }));
  } catch (diagnosticError) {
    cause = new AggregateError([cause, diagnosticError], "进程操作与诊断输出均失败", { cause });
  }
  return Object.assign(new Error(message, { cause }), { code: primary.code });
}

export function parseLinuxProcStat(raw: string): ProcessStat {
  const endOfCommand = raw.lastIndexOf(")");
  if (endOfCommand === -1) {
    throw new Error(`无法解析 /proc stat: ${raw}`);
  }
  const pid = Number(raw.slice(0, raw.indexOf(" ")));
  const fields = raw.slice(endOfCommand + 2).trim().split(/\s+/);
  return {
    pid,
    state: fields[0],
    ppid: Number(fields[1]),
    processGroupId: Number(fields[2]),
    processSessionId: Number(fields[3]),
    processStartTime: Number(fields[19]),
  };
}

export function parsePosixPsStat(raw: string, { numericSession = true }: { numericSession?: boolean } = {}): ProcessStat | null {
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

export function parsePosixSessionProcesses(raw: string, processSessionId: number | string): number[] {
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

function isValidProcessSessionId(processSessionId: unknown): processSessionId is number | string {
  return (
    (typeof processSessionId === "number" && Number.isInteger(processSessionId) && processSessionId > 0)
    || (
      typeof processSessionId === "string"
      && processSessionId.length > 0
    )
  );
}

async function readPosixProcessStat(pid: number): Promise<ProcessStat | null> {
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
  } catch (caught) {
    const error = caught as ProcessOperationFailure;
    let exists;
    try {
      exists = processExists(pid);
    } catch (probeError) {
      throw processOperationError("read-stat", error, { pid }, new AggregateError(
        [error, probeError], "进程身份读取与存在性核实均失败", { cause: error },
      ));
    }
    if (!exists) {
      return null;
    }
    throw processOperationError("read-stat", error, { pid });
  }
}

/** Windows 或进程已消失时返回 null。 */
export async function readProcessStat(pid: number): Promise<ProcessStat | null> {
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
  } catch (caught) {
    const error = caught as ProcessOperationFailure;
    if (error?.code === "ENOENT" || error?.code === "ESRCH") {
      return null;
    }
    throw processOperationError("read-stat", error, { pid });
  }
}

export function processExists(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch (caught) {
    const error = caught as ProcessOperationFailure;
    if (error?.code === "ESRCH") {
      return false;
    }
    if (error?.code === "EPERM") {
      return true;
    }
    throw processOperationError("probe", error, { pid });
  }
}

function signalProcess(pid: number, signal: NodeJS.Signals) {
  try {
    process.kill(pid, signal);
    return true;
  } catch (caught) {
    const error = caught as ProcessOperationFailure;
    if (error?.code === "ESRCH") {
      return false;
    }
    throw processOperationError("signal", error, { pid, signal });
  }
}

async function requestWindowsProcessTreeTermination(pid: number) {
  try {
    await execFileAsync("taskkill", ["/T", "/F", "/PID", String(pid)], {
      encoding: "utf8",
      windowsHide: true,
      timeout: TERMINATION_GRACE_MS,
    });
  } catch (caught) {
    const error = caught as ProcessOperationFailure;
    if (typeof error.code === "string") {
      throw processOperationError("taskkill", error, { pid });
    }
    let exists;
    try {
      exists = error.killed || processExists(pid);
    } catch (probeError) {
      throw processOperationError("taskkill", error, { pid }, new AggregateError(
        [error, probeError], "进程树终止与存在性核实均失败", { cause: error },
      ));
    }
    if (exists) {
      throw processOperationError("taskkill", error, { pid });
    }
  }
}

async function currentProcessSessionId() {
  return (await readProcessStat(process.pid))?.processSessionId ?? null;
}

async function processIdsInOsSession(processSessionId: number | string | null): Promise<number[]> {
  if (!isValidProcessSessionId(processSessionId)) {
    return [];
  }
  const callerProcessSessionId = await currentProcessSessionId();
  if (callerProcessSessionId === processSessionId) {
    return [];
  }
  if (process.platform !== "linux") {
    // TODO: 平台兼容：保持与单进程元数据查询相同的 Darwin sess / POSIX sid 语义。
    const sessionField = process.platform === "darwin" ? "sess=" : "sid=";
    let stdout;
    try {
      ({ stdout } = await execFileAsync("ps", ["-ax", "-o", "pid=", "-o", sessionField], { encoding: "utf8" }));
    } catch (caught) {
    const error = caught as ProcessOperationFailure;
      throw processOperationError("scan-session", error, { processSessionId });
    }
    return parsePosixSessionProcesses(stdout, processSessionId);
  }
  let entries;
  try {
    // procfs PID目录会随进程退出立即消失；只枚举名称，避免带类型枚举对每项做lstat时把这类竞态变成整次扫描失败。
    entries = await readdir("/proc");
  } catch (caught) {
    const error = caught as ProcessOperationFailure;
    throw processOperationError("scan-session", error, { processSessionId });
  }
  const processIds = entries
    .filter((entry) => /^\d+$/.test(entry))
    .map(Number)
    .filter((pid) => pid !== process.pid);
  const pids = [];
  for (let offset = 0; offset < processIds.length; offset += PROCESS_SCAN_BATCH_SIZE) {
    const stats = await Promise.all(
      processIds.slice(offset, offset + PROCESS_SCAN_BATCH_SIZE).map(readProcessStat),
    );
    for (const stat of stats) {
      if (isLiveProcess(stat) && stat.processSessionId === processSessionId) {
        pids.push(stat.pid);
      }
    }
  }
  return pids.sort((left, right) => right - left);
}

function isLiveProcess(stat: ProcessStat | null): stat is ProcessStat {
  // 僵尸和死亡进程已停止执行，等待父进程回收不属于终端的停止职责。
  return stat !== null && !["Z", "X", "x"].includes(stat.state ?? "");
}

async function hasLiveProcesses(pids: readonly number[]) {
  for (const pid of pids) {
    const isLive = process.platform === "linux"
      ? isLiveProcess(await readProcessStat(pid))
      : processExists(pid);
    if (isLive) {
      return true;
    }
  }
  return false;
}

async function waitForProcessesExit(pids: readonly number[], timeoutMs: number) {
  const deadlinePerformanceMs = performance.now() + timeoutMs;
  while (performance.now() < deadlinePerformanceMs) {
    if (!await hasLiveProcesses(pids)) {
      return true;
    }
    const remainingMs = deadlinePerformanceMs - performance.now();
    if (remainingMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, Math.min(100, remainingMs)));
    }
  }
  return !await hasLiveProcesses(pids);
}

async function terminalProcessIds({
  pid,
  processSessionId = null,
  processStartTime = null,
}: ProcessIdentity) {
  const stat = await readProcessStat(pid);
  if (processStartTime && stat?.processStartTime && stat.processStartTime !== processStartTime) {
    return [];
  }
  const effectiveProcessSessionId = processSessionId || stat?.processSessionId || null;
  const sessionPids = await processIdsInOsSession(effectiveProcessSessionId);
  if (sessionPids.length > 0) {
    return sessionPids;
  }
  return isLiveProcess(stat) ? [pid] : [];
}

export async function terminateProcessTree({
  pid,
  processSessionId = null,
  processStartTime = null,
}: {
  pid: number;
  processSessionId?: number | string | null;
  processStartTime?: number | string | null;
}): Promise<"terminated" | "force_killed" | "still_running" | "missing"> {
  if (process.platform === "win32") {
    if (!processExists(pid)) return "missing";
    await requestWindowsProcessTreeTermination(pid);
    return await waitForProcessesExit([pid], TERMINATION_GRACE_MS)
      ? "terminated"
      : "still_running";
  }

  const initialStat = await readProcessStat(pid);
  const processIdentity = {
    pid,
    processSessionId: processSessionId || initialStat?.processSessionId || null,
    processStartTime: processStartTime || initialStat?.processStartTime || null,
  };
  const termTargets = await terminalProcessIds(processIdentity);
  if (termTargets.length === 0) {
    return "missing";
  }

  for (const targetPid of termTargets) {
    signalProcess(targetPid, "SIGTERM");
  }
  await waitForProcessesExit(termTargets, TERMINATION_GRACE_MS);
  // 已捕获的 PID 退出不证明整个 session 停止，信号处理期间仍可能创建后代。
  const killTargets = await terminalProcessIds(processIdentity);
  if (killTargets.length === 0) {
    return "terminated";
  }

  for (const targetPid of killTargets) {
    signalProcess(targetPid, "SIGKILL");
  }
  await waitForProcessesExit(killTargets, TERMINATION_GRACE_MS);
  return (await terminalProcessIds(processIdentity)).length > 0
    ? "still_running"
    : "force_killed";
}
