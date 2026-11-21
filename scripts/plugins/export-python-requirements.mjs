// 产品发行只消费普通 wheel；开发环境的 editable 来源不进入发行依赖。
import { appendFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { buildPluginSdk } from "./build-sdk.mjs";

const root = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const [output] = process.argv.slice(2);
if (!output) throw new Error("请指定产品 Python 依赖清单输出路径");
const sdk = buildPluginSdk({ projectRoot: root, language: "python" });
const result = Bun.spawnSync(["uv", "export", "--locked", "--no-dev", "--no-emit-project",
  "--no-emit-package", "boxteam-plugin-sdk", "--format", "requirements-txt", "--output-file", output],
  { cwd: root, env: process.env, stdout: "inherit", stderr: "inherit" });
if (result.exitCode !== 0) throw new Error(`产品 Python 依赖导出失败: exit=${result.exitCode}`);
appendFileSync(path.resolve(root, output),
  `\nboxteam-plugin-sdk @ ${pathToFileURL(sdk.archive).href} --hash=sha256:${sdk.digest}\n`);
