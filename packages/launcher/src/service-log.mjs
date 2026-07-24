import { chmodSync, closeSync, mkdirSync, openSync, writeSync } from "node:fs";
import path from "node:path";

export const SERVICE_LOG_PATH_ENV = "BOXTEAM_SERVICE_LOG_PATH";
export const SERVICE_LOG_CAPTURED_ENV = "BOXTEAM_SERVICE_LOG_CAPTURED";

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

  mkdirSync(path.dirname(logPath), { recursive: true, mode: 0o700 });
  const descriptor = openSync(logPath, "a", 0o600);
  try {
    chmodSync(logPath, 0o600);
  } catch (error) {
    closeSync(descriptor);
    throw error;
  }
  const tee = (primary) => ({
    write(value) {
      const content = Buffer.isBuffer(value)
        ? value
        : Buffer.from(String(value));
      writeSync(descriptor, content);
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
