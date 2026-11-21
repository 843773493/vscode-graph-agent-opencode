import path from "node:path";
import { spawnSync } from "node:child_process";
import { resolveDevelopmentLayout } from "./development/environment.mjs";

// 先准备运行 JS，再加载依赖 SDK 的启动器；不启动额外的 watch 服务。
export async function main(args = process.argv.slice(2)) {
  const action = args[0];
  const managed = action !== undefined && !action.startsWith("-");
  if (managed && !["start", "stop", "status"].includes(action)) {
    throw new Error(`未知 development manager 操作: ${action}`);
  }
  if (!managed || action === "start") {
    const { frontendRoot, projectRoot } = resolveDevelopmentLayout({ args });
    const node = process.env.NODE_BIN ?? (process.platform === "win32" ? "node.exe" : "node");
    for (const root of new Set([frontendRoot, projectRoot])) {
      const result = spawnSync(node, [path.join(root, "sdk/node/build.mjs")], {
        cwd: root, stdio: "inherit", windowsHide: true,
      });
      if (result.error) throw result.error;
      if (result.status !== 0 || result.signal) {
        throw new Error(`开发 SDK 构建失败: root=${root} exit=${result.status} signal=${result.signal}`);
      }
    }
  }
  if (managed) await (await import("./development/manager.mjs")).main(args);
  else await import("./development/services.mjs");
}

if (import.meta.main) await main();
