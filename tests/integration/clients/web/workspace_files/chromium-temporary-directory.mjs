import { mkdtemp, mkdir, open, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const temporaryEnvironmentNames = ["TMPDIR", "TMP", "TEMP"];

function restoreTemporaryEnvironment(temporaryDirectory, originalEnvironment) {
  for (const name of temporaryEnvironmentNames) {
    if (process.env[name] !== temporaryDirectory) continue;
    const originalValue = originalEnvironment.get(name);
    if (originalValue === undefined) delete process.env[name];
    else process.env[name] = originalValue;
  }
}

async function releaseTemporaryDirectoryResources(
  directoryHandle,
  aliasDirectory,
  restoreEnvironment,
) {
  const errors = [];
  if (restoreEnvironment) {
    try {
      restoreEnvironment();
    } catch (error) {
      errors.push(error);
    }
  }
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
    } else if (process.platform === "darwin" || process.platform === "win32") {
      // macOS 使用 /tmp；Windows 使用系统临时目录并通过 junction 避免权限要求。
      const aliasRoot = process.platform === "darwin" ? "/tmp" : tmpdir();
      aliasDirectory = await mkdtemp(join(aliasRoot, "boxteam-chromium-"));
      temporaryDirectory = join(aliasDirectory, "runtime");
      await symlink(
        absoluteRuntimeDirectory,
        temporaryDirectory,
        process.platform === "win32" ? "junction" : "dir",
      );
    }
  } catch (setupError) {
    try {
      await releaseTemporaryDirectoryResources(directoryHandle, aliasDirectory);
    } catch (cleanupError) {
      throw new AggregateError([setupError, cleanupError], "设置 Chromium 临时目录失败并且清理资源时出错");
    }
    throw setupError;
  }

  const originalEnvironment = new Map(
    temporaryEnvironmentNames.map((name) => [name, process.env[name]]),
  );
  for (const name of temporaryEnvironmentNames) {
    process.env[name] = temporaryDirectory;
  }

  return () => releaseTemporaryDirectoryResources(
    directoryHandle,
    aliasDirectory,
    () => restoreTemporaryEnvironment(temporaryDirectory, originalEnvironment),
  );
}
