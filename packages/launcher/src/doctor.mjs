import { existsSync } from "node:fs";

import { readLauncherLock } from "./instance-lock.mjs";

export function buildDoctorReport({ runtime, boxteamHome }) {
  return {
    ok: true,
    distribution: runtime.distribution,
    version: runtime.version,
    platform: process.platform,
    architecture: process.arch,
    boxteam_home: boxteamHome,
    manifest_path: runtime.manifestPath,
    runtime_root: runtime.runtimeRoot,
    python: {
      executable: runtime.pythonExecutable,
      exists: existsSync(runtime.pythonExecutable),
    },
    node: {
      source: runtime.node.source,
      executable: runtime.nodeExecutable,
      version: process.version,
      exists: existsSync(runtime.nodeExecutable),
    },
    web_assets:
      runtime.webAssets === null
        ? null
        : {
            path: runtime.webAssets,
            exists: existsSync(runtime.webAssets),
          },
    chromium:
      runtime.chromiumExecutable === null
        ? null
        : {
            executable: runtime.chromiumExecutable,
            exists: existsSync(runtime.chromiumExecutable),
          },
    launcher_lock: readLauncherLock(boxteamHome),
  };
}

export function printDoctorReport(report, { json, stdout = process.stdout }) {
  if (json) {
    stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  stdout.write(`BoxTeam ${report.version} (${report.distribution})\n`);
  stdout.write(`平台: ${report.platform}-${report.architecture}\n`);
  stdout.write(`BOXTEAM_HOME: ${report.boxteam_home}\n`);
  stdout.write(`Manifest: ${report.manifest_path}\n`);
  stdout.write(`Python: ${report.python.executable}\n`);
  stdout.write(`Node: ${report.node.executable} ${report.node.version}\n`);
  stdout.write(
    `Launcher: ${
      report.launcher_lock === null
        ? "未运行"
        : `pid=${String(report.launcher_lock.value.pid)}`
    }\n`,
  );
}
