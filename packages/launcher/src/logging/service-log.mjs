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

export const SERVICE_LOG_PATH_ENV = "BOXTEAM_SERVICE_LOG_PATH";
export const SERVICE_LOG_CAPTURED_ENV = "BOXTEAM_SERVICE_LOG_CAPTURED";
export const SERVICE_LOG_MAX_BYTES = 64 * 1024 * 1024;
export const SERVICE_LOG_ROTATIONS = 3;

export function rotateServiceLog(logPath) {
  let logStat;
  try {
    logStat = statSync(logPath);
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  if (logStat.size < SERVICE_LOG_MAX_BYTES) return;
  for (let index = SERVICE_LOG_ROTATIONS - 1; index >= 1; index -= 1) {
    const source = `${logPath}.${index}`;
    const target = `${logPath}.${index + 1}`;
    if (existsSync(source)) renameSync(source, target);
  }
  renameSync(logPath, `${logPath}.1`);
}

export function resolveServiceLogPath(boxteamHome, environment = process.env) {
  const configured = environment[SERVICE_LOG_PATH_ENV]?.trim();
  if (configured) return path.resolve(configured);
  return path.join(
    path.resolve(boxteamHome),
    "state",
    "launcher",
    "logs",
    "services.log",
  );
}

/** 打开并初始化日志描述符；成功后由调用方负责关闭。 */
export function openServiceLogDescriptor(logPath) {
  mkdirSync(path.dirname(logPath), { recursive: true, mode: 0o700 });
  rotateServiceLog(logPath);
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
  stdout = process.stdout,
  stderr = process.stderr,
}) {
  const logPath = resolveServiceLogPath(boxteamHome, environment);
  if (environment[SERVICE_LOG_CAPTURED_ENV] === "1") {
    return { path: logPath, stdout, stderr, close() {} };
  }
  const descriptor = openServiceLogDescriptor(logPath);
  const tee = (primary) => ({
    write(value) {
      const content = Buffer.isBuffer(value)
        ? value
        : Buffer.from(String(value));
      writeFileSync(descriptor, content);
      return primary.write(value);
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
