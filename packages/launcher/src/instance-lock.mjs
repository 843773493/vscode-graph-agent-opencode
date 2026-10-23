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

export function processIsAlive(pid, killImpl = process.kill) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    killImpl(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    // EPERM表示目标存在但无权发信号；其它查询错误不能伪装成存活。
    if (error?.code === "EPERM") return true;
    throw error;
  }
}

export function removeLauncherLock(boxteamHome, token) {
  const current = readLauncherLock(boxteamHome);
  if (current?.value?.token !== token) return false;
  rmSync(current.path);
  return true;
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
    } catch (error) {
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

    const failures = [];
    try {
      writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, "utf8");
    } catch (error) {
      failures.push(error);
    }
    try {
      closeSync(descriptor);
    } catch (error) {
      failures.push(error);
    }
    if (failures.length > 0) {
      // 仅清理本次排他创建且尚未交付的锁；碰到其它实例的锁不进入此分支。
      try {
        rmSync(lockPath);
      } catch (error) {
        failures.push(error);
      }
      if (failures.length === 1) throw failures[0];
      throw new AggregateError(
        failures,
        `初始化 Launcher 锁失败，资源释放也失败: ${lockPath}`,
        { cause: failures[0] },
      );
    }
    return {
      path: lockPath,
      value,
      release() {
        removeLauncherLock(boxteamHome, token);
      },
    };
  }
  throw new Error(`无法获取 Launcher 锁: ${lockPath}`);
}
