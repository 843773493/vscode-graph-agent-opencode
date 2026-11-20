// 自举入口只依赖标准库和归档构建器，不能导入待安装 SDK 或产品模块。
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { buildPluginSdk } from "./build-sdk.mjs";

const root = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const args = process.argv.slice(2);
const check = args.includes("--check");
for (const language of args.filter(argument => argument !== "--check")) {
  const sdk = buildPluginSdk({ projectRoot: root, language });
  const archive = path.relative(root, sdk.archive).split(path.sep).join("/");
  if (language === "node") {
    const file = path.join(root, "package.json");
    const manifest = JSON.parse(readFileSync(file, "utf8"));
    if (check) {
      if (manifest.dependencies["@boxteam/plugin-sdk"] !== `file:${archive}`) throw new Error("Node SDK 来源过期；先在开发 checkout 准备 SDK 并提交依赖清单及锁");
      continue;
    }
    manifest.dependencies["@boxteam/plugin-sdk"] = `file:${archive}`;
    writeFileSync(file, JSON.stringify(manifest, null, 2) + "\n");
  } else {
    const file = path.join(root, "pyproject.toml");
    const original = readFileSync(file, "utf8");
    const source = original
      .replace(/"boxteam-plugin-sdk==[^"\n]+"/, `"boxteam-plugin-sdk==${sdk.version}"`)
      .replace(/^boxteam-plugin-sdk = \{ path = "[^"\n]+" \}$/m, `boxteam-plugin-sdk = { path = "${archive}" }`);
    if (check) {
      if (original !== source) throw new Error("Python SDK 来源过期；先在开发 checkout 准备 SDK 并提交依赖清单及锁");
    } else writeFileSync(file, source);
  }
}
