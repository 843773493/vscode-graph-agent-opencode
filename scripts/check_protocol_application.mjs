// 应用合同必须消费已正常安装的当前 SDK；纯生成/check 不执行本入口。
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";

const root = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const stagingRoot = path.join(root, "out/protocol-application");
await mkdir(stagingRoot, { recursive: true });
const stage = await mkdtemp(path.join(stagingRoot, "check-"));
const python = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit", env: process.env });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`应用协议校验失败: ${command} ${args.join(" ")} exit=${result.status}`);
}
try {
  const descriptor = path.join(stage, "protocol.binpb");
  const buf = createRequire(path.join(root, "package.json")).resolve("@bufbuild/buf/bin/buf");
  run(process.execPath, [buf, "build", "--as-file-descriptor-set", "--output", descriptor]);
  run(python, ["-m", "scripts.export_openapi_snapshot", "--validate-only", "--descriptor-set", descriptor]);
  run(process.execPath, [path.join(root, "scripts/generate_sse_runtime.mjs"), "--output-root", stage, "--descriptor-set", descriptor]);
  for (const file of ["src/clients/web/src/types/protocol_generated/sse_runtime_schemas.json",
    "src/shared/sseRuntimeValidators.js", "src/shared/sseRuntimeValidators.d.ts"]) {
    if (!(await readFile(path.join(root, file))).equals(await readFile(path.join(stage, file)))) {
      throw new Error(`应用生成产物过期: ${file}；安装当前 SDK 后运行 bun scripts/generate_sse_runtime.mjs`);
    }
  }
} finally {
  await rm(stage, { recursive: true, force: true });
}
