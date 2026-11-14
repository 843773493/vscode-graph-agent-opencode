import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { stopLauncher } from "./stop.mjs";

export async function stopLauncherForSupportedUpgrade({
  runtime, environment, targetLock, spawnSyncImpl = spawnSync, stopLauncherImpl = stopLauncher,
}) {
  const preview = initializeUserConfiguration({
    runtime, environment, args: ["initialize", "--preview"], spawnSyncImpl,
  });
  const inspection = JSON.parse(preview.output);
  if (inspection.upgrade_required) {
    return stopLauncherImpl({ boxteamHome: preview.boxteamHome, expectedToken: targetLock.value.token });
  }
  return null;
}

export function defaultBoxteamHome(distribution, homeDirectory = os.homedir()) {
  return path.join(
    homeDirectory,
    distribution === "source-development" ? ".boxteams-dev" : ".boxteams",
  );
}

export function initializeUserConfiguration({
  runtime,
  environment,
  args = [],
  spawnSyncImpl = spawnSync,
}) {
  const boxteamHome =
    environment.BOXTEAM_HOME?.trim() ||
    defaultBoxteamHome(runtime.distribution);
  const childEnvironment = {
    ...environment,
    BOXTEAM_HOME: boxteamHome,
    // stdout JSON 与下方解码共用UTF-8，不依赖目标终端的默认代码页。
    PYTHONIOENCODING: "utf-8",
    PYTHONPATH: [
      runtime.applicationRoot,
      environment.PYTHONPATH,
    ]
      .filter(Boolean)
      .join(path.delimiter),
  };
  const result = spawnSyncImpl(
    runtime.pythonExecutable,
    ["-m", "configs.boxteam", ...args],
    {
      cwd: runtime.applicationRoot,
      env: childEnvironment,
      encoding: "utf8",
    },
  );
  if (result.error) {
    throw new Error(
      `启动内置 Python 配置初始化失败: ${runtime.pythonExecutable}: ${result.error.message}`,
      { cause: result.error },
    );
  }
  if (result.status !== 0) {
    throw new Error(
      `用户配置初始化失败: exit=${String(result.status)}\n` +
        `${String(result.stderr ?? "").trim()}`,
    );
  }
  return {
    boxteamHome,
    environment: childEnvironment,
    output: String(result.stdout ?? "").trim(),
  };
}
