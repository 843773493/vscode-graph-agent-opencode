import { rmSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const scriptRuntime = process.platform === "win32" ? "node" : process.execPath;
const packagingOutputRoot = path.join(
  projectRoot,
  "out",
  "packaging",
  "windows-x64",
);

const scripts = ["build-windows-x64.mjs", "verify-windows-x64.mjs"];
for (const [index, script] of scripts.entries()) {
  if (index === 1) {
    // 打包验证只需要最终 tarball、Release asset 和 ZIP；清理中间目录以便低磁盘空间的 Windows 用户机也能完成验证。
    rmSync(path.join(packagingOutputRoot, "stage"), {
      recursive: true,
      force: true,
    });
    rmSync(path.join(packagingOutputRoot, "downloads"), {
      recursive: true,
      force: true,
    });
  }
  const result = spawnSync(
    scriptRuntime,
    [path.join(projectRoot, "packaging", "runtime", script)],
    {
      cwd: projectRoot,
      env: process.env,
      stdio: "inherit",
    },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${script} 失败: exit=${String(result.status)}`);
  }
}
