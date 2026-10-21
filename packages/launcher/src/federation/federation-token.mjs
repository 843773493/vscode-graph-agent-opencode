import path from "node:path";
import { spawnSync } from "node:child_process";

export function issueFederationToken({
  runtime,
  environment,
  args,
  spawnSyncImpl = spawnSync,
}) {
  const result = spawnSyncImpl(
    runtime.pythonExecutable,
    ["-m", "app.gateway.federation_pairing", ...args],
    {
      cwd: runtime.applicationRoot,
      env: {
        ...environment,
        PYTHONPATH: [runtime.applicationRoot, environment.PYTHONPATH]
          .filter(Boolean)
          .join(path.delimiter),
      },
      encoding: "utf8",
    },
  );
  if (result.error) {
    throw new Error(
      `签发 Gateway 配对令牌失败: ${runtime.pythonExecutable}: ${result.error.message}`,
    );
  }
  if (result.status !== 0) {
    throw new Error(
      `签发 Gateway 配对令牌失败: exit=${String(result.status)}\n` +
        `${String(result.stderr ?? "").trim()}`,
    );
  }
  return String(result.stdout ?? "").trim();
}
