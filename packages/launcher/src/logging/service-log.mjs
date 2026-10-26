import {
  chmodSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { loadRuntimeLogContract, runtimeLogPath, runtimeLogRoot } from "./runtime-contract.mjs";

export const SERVICE_LOG_PATH_ENV = "BOXTEAM_SERVICE_LOG_PATH";
export const SERVICE_LOG_CAPTURED_ENV = "BOXTEAM_SERVICE_LOG_CAPTURED";

export function rotateServiceLog(logPath, contract = loadRuntimeLogContract()) {
  let logStat;
  try {
    logStat = statSync(logPath);
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  if (logStat.size < contract.rotation.max_bytes) return;
  for (let index = contract.rotation.archives - 1; index >= 1; index -= 1) {
    const source = `${logPath}.${index}`;
    const target = `${logPath}.${index + 1}`;
    if (existsSync(source)) renameSync(source, target);
  }
  renameSync(logPath, `${logPath}.1`);
}

export function resolveServiceLogPath(boxteamHome, environment = process.env, role = "launcher") {
  const configured = role === "launcher" ? environment[SERVICE_LOG_PATH_ENV]?.trim() : null;
  if (configured) return path.resolve(configured);
  const contract = loadRuntimeLogContract(environment.BOXTEAM_PROJECT_ROOT);
  return runtimeLogPath(runtimeLogRoot(boxteamHome, contract), role, null, contract);
}

/** 打开并初始化日志描述符；成功后由调用方负责关闭。 */
export function openServiceLogDescriptor(logPath, contract = loadRuntimeLogContract()) {
  mkdirSync(path.dirname(logPath), { recursive: true, mode: 0o700 });
  rotateServiceLog(logPath, contract);
  const descriptor = openSync(logPath, "a", 0o600);
  // TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
  if (process.platform !== "win32") {
    try {
      chmodSync(logPath, 0o600);
    } catch (error) {
      try {
        closeSync(descriptor);
      } catch (closeError) {
        throw new AggregateError(
          [error, closeError],
          `初始化服务日志失败，描述符释放也失败: ${logPath}`,
          { cause: error },
        );
      }
      throw error;
    }
  }
  return descriptor;
}

export function openServiceLog({
  boxteamHome,
  environment = process.env,
  role = "launcher",
  stdout = process.stdout,
  stderr = process.stderr,
}) {
  const logPath = resolveServiceLogPath(boxteamHome, environment, role);
  const captured = environment[SERVICE_LOG_CAPTURED_ENV] === "1";
  if (role === "launcher" && captured) {
    return { path: logPath, stdout, stderr, close() {} };
  }
  const descriptor = openServiceLogDescriptor(logPath, loadRuntimeLogContract(environment.BOXTEAM_PROJECT_ROOT));
  const tee = (primary) => ({
    write(value) {
      const content = Buffer.isBuffer(value)
        ? value
        : Buffer.from(String(value));
      writeFileSync(descriptor, content);
      // detached 的前台流已落入 Launcher 文件，不能再把 Gateway 输出写入那里。
      return captured ? true : primary.write(value);
    },
  });
  return {
    path: logPath,
    stdout: tee(stdout),
    stderr: tee(stderr),
    close() {
      closeSync(descriptor);
    },
  };
}
