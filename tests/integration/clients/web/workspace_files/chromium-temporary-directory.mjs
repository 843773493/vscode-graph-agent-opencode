import { mkdtemp, mkdir, open, rm, symlink } from "node:fs/promises";
import { join, resolve } from "node:path";

const temporaryEnvironmentNames = ["TMPDIR", "TMP", "TEMP"];

async function releaseTemporaryDirectoryResources(directoryHandle, aliasDirectory) {
  const errors = [];
  if (directoryHandle) {
    try {
      await directoryHandle.close();
    } catch (error) {
      errors.push(error);
    }
  }
  if (aliasDirectory) {
    try {
      await rm(aliasDirectory, { recursive: true });
    } catch (error) {
      errors.push(error);
    }
  }
  if (errors.length > 0) {
    throw new AggregateError(errors, "清理 Chromium 临时目录失败");
  }
}

export async function configureChromiumTemporaryDirectory(runtimeDirectory) {
  const absoluteRuntimeDirectory = resolve(runtimeDirectory);
  await mkdir(absoluteRuntimeDirectory, { recursive: true });

  let directoryHandle;
  let aliasDirectory;
  let temporaryDirectory = absoluteRuntimeDirectory;
  try {
    if (process.platform === "linux") {
      directoryHandle = await open(absoluteRuntimeDirectory, "r");
      temporaryDirectory = `/proc/${process.pid}/fd/${directoryHandle.fd}`;
    } else if (process.platform === "darwin") {
      // macOS 的 TMPDIR 可能较长；系统 /tmp 提供稳定的短别名根目录。
      aliasDirectory = await mkdtemp(join("/tmp", "boxteam-chromium-"));
      temporaryDirectory = join(aliasDirectory, "runtime");
      await symlink(absoluteRuntimeDirectory, temporaryDirectory, "dir");
    }
  } catch (setupError) {
    try {
      await releaseTemporaryDirectoryResources(directoryHandle, aliasDirectory);
    } catch (cleanupError) {
      throw new AggregateError([setupError, cleanupError], "设置 Chromium 临时目录失败并且清理资源时出错");
    }
    throw setupError;
  }

  for (const name of temporaryEnvironmentNames) {
    process.env[name] = temporaryDirectory;
  }

  return () => releaseTemporaryDirectoryResources(directoryHandle, aliasDirectory);
}
