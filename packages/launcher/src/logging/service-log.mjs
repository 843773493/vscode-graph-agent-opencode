import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  chmodSync, closeSync, constants, existsSync, fstatSync, fsyncSync, lstatSync,
  mkdirSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync,
} from "node:fs";
import path from "node:path";
import { createLocker, Lock } from "@lickle/lock";

export const SERVICE_LOG_PATH_ENV = "BOXTEAM_SERVICE_LOG_PATH";
export const SERVICE_LOG_CAPTURED_ENV = "BOXTEAM_SERVICE_LOG_CAPTURED";
const nativePlatform = process.platform;
const fileLock = createLocker();

function rejectSymbolicLink(file) {
  try {
    if (lstatSync(file).isSymbolicLink()) throw new Error(`日志文件不允许是符号链接: ${file}`);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}

async function withLogGuard(logPath, operation) {
  const guardPath = `${logPath}.owner.lock`;
  rejectSymbolicLink(guardPath);
  rejectSymbolicLink(`${logPath}.owner.json`);
  const descriptor = openSync(guardPath, constants.O_RDWR | constants.O_CREAT | (constants.O_NOFOLLOW ?? 0), 0o600);
  const failures = [];
  let file;
  try {
    if (!await fileLock.tryLock(descriptor, Lock.Exclusive)) throw new Error(`日志所有权正在变更: ${logPath}`);
    file = await operation();
  } catch (error) { failures.push(error); }
  try { closeSync(descriptor); }
  catch (error) { failures.push(error); }
  if (failures.length && file) {
    // 锁释放失败时不交付文件，仍需释放本次已打开的日志描述符。
    try { file.close(); }
    catch (error) { failures.push(error); }
  }
  if (failures.length === 1) throw failures[0];
  if (failures.length > 1) throw new AggregateError(failures, `日志操作与所有权释放均失败: ${logPath}`, { cause: failures[0] });
  return file;
}

function windowsLogHasOpenHandles(logPath) {
  const script = `
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    try {
      $stream = [IO.File]::Open($env:BOXTEAM_LOG_PROBE_PATH, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
      $stream.Dispose()
      exit 0
    } catch {
      $errorCause = $_.Exception.GetBaseException()
      if (($errorCause -is [IO.IOException]) -and (($errorCause.HResult -band 65535) -in @(32, 33))) { exit 32 }
      [Console]::Error.WriteLine($errorCause.ToString()); exit 1
    }
  `;
  const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")], {
    env: { ...process.env, BOXTEAM_LOG_PROBE_PATH: path.toNamespacedPath(logPath) },
    encoding: "utf8", windowsHide: true, timeout: 5000,
  });
  if (result.error) throw new Error(`无法核实日志共享句柄: ${logPath}`, { cause: result.error });
  if (result.status === 32) return true;
  if (result.status !== 0) throw new Error(`核实日志共享句柄失败: ${logPath}: ${result.stderr}`);
  return false;
}

function publishLogOwnership(logPath, sink) {
  const temporary = `${logPath}.owner.json.${randomUUID()}.tmp`;
  const failures = [];
  let descriptor;
  try {
    descriptor = openSync(temporary, "wx", 0o600);
    writeFileSync(descriptor, JSON.stringify({ version: 1, phase: "stream", pid: process.pid, ...sink }));
    fsyncSync(descriptor);
  } catch (error) { failures.push(error); }
  if (descriptor !== undefined) {
    try { closeSync(descriptor); }
    catch (error) { failures.push(error); }
  }
  if (!failures.length) {
    try { renameSync(temporary, `${logPath}.owner.json`); }
    catch (error) { failures.push(error); }
  }
  if (descriptor !== undefined && existsSync(temporary)) {
    try { unlinkSync(temporary); }
    catch (error) { failures.push(error); }
  }
  if (failures.length === 1) throw failures[0];
  if (failures.length > 1) throw new AggregateError(failures, `日志归属写入与清理均失败: ${logPath}`, { cause: failures[0] });
}

function rotateServiceLog(logPath, contract) {
  for (let index = contract.rotation.archives - 1; index >= 1; index -= 1) {
    const source = `${logPath}.${index}`;
    const target = `${logPath}.${index + 1}`;
    rejectSymbolicLink(source);
    rejectSymbolicLink(target);
    if (existsSync(source)) renameSync(source, target);
  }
  rejectSymbolicLink(`${logPath}.1`);
  renameSync(logPath, `${logPath}.1`);
}

export function resolveServiceLogPath(boxteamHome, environment, contract, role = "launcher") {
  const configured = role === "launcher" ? environment[SERVICE_LOG_PATH_ENV]?.trim() : null;
  if (configured) return path.resolve(configured);
  return contract.pathFor(contract.rootFor(boxteamHome), role);
}

/** 文件锁随实际 Unix 输出描述符保留；Windows 在同一临界区核对原生共享句柄。 */
export async function openServiceLogFile(logPath, contract, { role = "launcher", source = "default" } = {}) {
  logPath = path.resolve(logPath);
  mkdirSync(path.dirname(logPath), { recursive: true, mode: 0o700 });
  return withLogGuard(logPath, async () => {
    rejectSymbolicLink(logPath);
    let previous;
    try { previous = JSON.parse(readFileSync(`${logPath}.owner.json`, "utf8")); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    if (previous !== undefined && (previous.version !== 1 || previous.phase !== "stream")) {
      throw new Error(`日志已由其他进程 owner 登记，拒绝重开: ${logPath}`);
    }
    if (previous !== undefined) {
      const identity = statSync(logPath, { bigint: true });
      if (previous.path !== logPath || String(identity.dev) !== previous.device || String(identity.ino) !== previous.inode) {
        throw new Error(`原日志文件身份已变化，拒绝重开或轮转: ${logPath}`);
      }
    }
    if (nativePlatform === "win32" && existsSync(logPath) && windowsLogHasOpenHandles(logPath)) {
      throw new Error(`日志仍有活动写入者，拒绝重开或轮转: ${logPath}`);
    }
    const openLocked = async () => {
      const descriptor = openSync(logPath, constants.O_RDWR | constants.O_CREAT | constants.O_APPEND | (constants.O_NOFOLLOW ?? 0), 0o600);
      try {
        if (nativePlatform !== "win32" && !await fileLock.tryLock(descriptor, Lock.Exclusive)) {
          throw new Error(`日志仍有活动写入者，拒绝重开或轮转: ${logPath}`);
        }
        // TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
        if (process.platform !== "win32") chmodSync(logPath, 0o600);
        return descriptor;
      } catch (error) {
        try { closeSync(descriptor); }
        catch (closeError) { throw new AggregateError([error, closeError], `初始化服务日志失败，描述符释放也失败: ${logPath}`, { cause: error }); }
        throw error;
      }
    };
    let descriptor = await openLocked();
    try {
      const size = fstatSync(descriptor).size;
      // 已确认没有旧写入者；轮转期间由 guard 阻止其他受管 owner 插入。
      if (size >= contract.rotation.max_bytes) {
        const previousDescriptor = descriptor;
        descriptor = null;
        closeSync(previousDescriptor);
        rotateServiceLog(logPath, contract);
        descriptor = await openLocked();
      }
      const identity = fstatSync(descriptor, { bigint: true });
      const sink = Object.freeze({ path: logPath, owner_id: randomUUID(), role, source,
        device: String(identity.dev), inode: String(identity.ino) });
      publishLogOwnership(logPath, sink);
      return { descriptor, sink, close() { closeSync(descriptor); } };
    } catch (error) {
      if (descriptor !== null) {
        try { closeSync(descriptor); }
        catch (closeError) { throw new AggregateError([error, closeError], `初始化服务日志失败，描述符释放也失败: ${logPath}`, { cause: error }); }
      }
      throw error;
    }
  });
}

function capturedServiceLog(logPath, environment, stdout, stderr) {
  const sink = JSON.parse(environment.BOXTEAM_RUNTIME_LOG_SINK ?? "null");
  if (!sink || sink.path !== logPath || sink.role !== "launcher") throw new Error("外层日志捕获缺少冻结去向证明");
  const identities = [fstatSync(1, { bigint: true }), fstatSync(2, { bigint: true }), statSync(logPath, { bigint: true })];
  if (identities.some((value) => String(value.dev) !== sink.device || String(value.ino) !== sink.inode)) {
    throw new Error("外层日志捕获与实际输出描述符不匹配");
  }
  const record = JSON.parse(readFileSync(`${logPath}.owner.json`, "utf8"));
  if (record.owner_id !== sink.owner_id || record.phase !== "stream") throw new Error("外层日志捕获的 owner 已变化");
  return { path: logPath, sink, descriptors: [1, 2], stdout, stderr, close() {} };
}

export async function openServiceLog({ boxteamHome, contract, environment = process.env,
  role = "launcher", stdout = process.stdout, stderr = process.stderr }) {
  const logPath = resolveServiceLogPath(boxteamHome, environment, contract, role);
  const captured = environment[SERVICE_LOG_CAPTURED_ENV] === "1";
  if (role === "launcher" && captured) return capturedServiceLog(logPath, environment, stdout, stderr);
  const file = await openServiceLogFile(logPath, contract, { role,
    source: role === "launcher" && environment[SERVICE_LOG_PATH_ENV]?.trim() ? SERVICE_LOG_PATH_ENV : "default" });
  const tee = (primary) => ({
    write(value) {
      const content = Buffer.isBuffer(value) ? value : Buffer.from(String(value));
      writeFileSync(file.descriptor, content);
      return captured ? true : primary.write(value);
    },
  });
  return { path: logPath, sink: file.sink, descriptors: [file.descriptor],
    stdout: tee(stdout), stderr: tee(stderr), close: file.close };
}
