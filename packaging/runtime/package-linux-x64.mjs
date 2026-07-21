import path from "node:path";
import { spawnSync } from "node:child_process";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);

for (const script of ["build-linux-x64.mjs", "verify-linux-x64.mjs"]) {
  const result = spawnSync(
    process.execPath,
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
