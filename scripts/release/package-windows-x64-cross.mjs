import { spawnSync } from "node:child_process";
import path from "node:path";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const result = spawnSync(
  process.execPath,
  [path.join(projectRoot, "packaging", "runtime", "build-windows-x64.mjs"), "--cross"],
  {
    cwd: projectRoot,
    env: process.env,
    stdio: "inherit",
  },
);
if (result.error) throw result.error;
if (result.status !== 0) {
  throw new Error(`Linux Windows x64 交叉打包失败: exit=${String(result.status)}`);
}
