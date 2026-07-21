import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

export function launcherLockPath(boxteamHome) {
  return path.join(path.resolve(boxteamHome), "state", "launcher.lock");
}

export function readLauncherLock(boxteamHome) {
  const lockPath = launcherLockPath(boxteamHome);
  try {
    const value = JSON.parse(readFileSync(lockPath, "utf8"));
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new TypeError("锁文件根节点不是对象");
    }
    return { path: lockPath, value };
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw new Error(
      `读取 Launcher 锁失败: ${lockPath}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
}

function processIsAlive(pid, killImpl = process.kill) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    killImpl(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    return true;
  }
}

export function acquireLauncherLock({
  boxteamHome,
  runtime,
  processObject = process,
  killImpl = process.kill,
}) {
  const lockPath = launcherLockPath(boxteamHome);
  mkdirSync(path.dirname(lockPath), { recursive: true, mode: 0o700 });
  const token = randomUUID();
  const value = {
    schema_version: 1,
    token,
    pid: processObject.pid,
    started_at: new Date().toISOString(),
    distribution: runtime.distribution,
    version: runtime.version,
    manifest_path: runtime.manifestPath,
  };

  for (let attempt = 0; attempt < 2; attempt += 1) {
    let descriptor;
    try {
      descriptor = openSync(lockPath, "wx", 0o600);
      writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, "utf8");
      closeSync(descriptor);
      descriptor = undefined;
      return {
        path: lockPath,
        value,
        release() {
          const current = readLauncherLock(boxteamHome);
          if (current?.value?.token === token) {
            rmSync(lockPath);
          }
        },
      };
    } catch (error) {
      if (descriptor !== undefined) closeSync(descriptor);
      if (error?.code !== "EEXIST") throw error;
      const current = readLauncherLock(boxteamHome);
      if (
        current !== null &&
        processIsAlive(current.value.pid, killImpl)
      ) {
        throw new Error(
          `BOXTEAM_HOME 已由另一个 Launcher 使用: ${boxteamHome} ` +
            `(pid=${String(current.value.pid)}, ` +
            `distribution=${String(current.value.distribution)}, ` +
            `version=${String(current.value.version)})`,
        );
      }
      if (attempt === 0) {
        rmSync(lockPath);
        continue;
      }
      throw new Error(`无法替换已失效的 Launcher 锁: ${lockPath}`);
    }
  }
  throw new Error(`无法获取 Launcher 锁: ${lockPath}`);
}
