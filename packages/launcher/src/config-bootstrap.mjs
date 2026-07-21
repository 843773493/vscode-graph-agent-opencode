import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

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
    PYTHONPATH: [
      runtime.applicationRoot,
      environment.PYTHONPATH,
    ]
      .filter(Boolean)
      .join(path.delimiter),
  };
  if (runtime.distribution !== "source-development") {
    childEnvironment.BOXTEAM_INSTALL_DEVELOPMENT_ASSETS = "0";
    childEnvironment.BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE = "0";
  }
  const configArgs =
    runtime.distribution === "source-development"
      ? ["--project-root", runtime.applicationRoot, ...args]
      : args;
  const result = spawnSyncImpl(
    runtime.pythonExecutable,
    ["-m", "configs.boxteam", ...configArgs],
    {
      cwd: runtime.applicationRoot,
      env: childEnvironment,
      encoding: "utf8",
    },
  );
  if (result.error) {
    throw new Error(
      `启动内置 Python 配置初始化失败: ${runtime.pythonExecutable}: ${result.error.message}`,
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
