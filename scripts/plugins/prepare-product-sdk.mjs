// 开发依赖准备只同步元数据与生成资源，不构建 SDK 或导入产品模块。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const root = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const args = process.argv.slice(2);
const check = args.includes("--check");
for (const language of args.filter(argument => argument !== "--check")) {
  if (!["node", "python"].includes(language)) throw new Error(`未知 SDK 语言: ${language}`);
  const productManifest = path.join(root, "package.json");
  const product = JSON.parse(readFileSync(productManifest, "utf8"));
  const version = product.version;
  let generated;
  if (language === "node") {
    const sdkManifest = path.join(root, "sdk/node/package.json");
    product.workspaces = [...new Set([...(product.workspaces ?? []), "sdk/node"])];
    product.dependencies["@boxteam/plugin-sdk"] = "workspace:*";
    generated = [
      [sdkManifest, readFileSync(sdkManifest, "utf8").replace(/("version":\s*)"[^"]*"/, `$1"${version}"`)],
      [productManifest, JSON.stringify(product, null, 2) + "\n"],
    ];
  } else {
    const sdkManifest = path.join(root, "sdk/python/pyproject.toml");
    const pythonManifest = path.join(root, "pyproject.toml");
    generated = [
      [sdkManifest, readFileSync(sdkManifest, "utf8").replace(/^version = .*$/m, `version = "${version}"`)],
      [pythonManifest, readFileSync(pythonManifest, "utf8")
        .replace(/"boxteam-plugin-sdk==[^"\n]+"/, `"boxteam-plugin-sdk==${version}"`)
        .replace(/^boxteam-plugin-sdk = \{.*\}$/m, 'boxteam-plugin-sdk = { path = "sdk/python", editable = true }')],
    ];
  }
  const resource = language === "node" ? "sdk/node/src/resources/logging" : "sdk/python/src/boxteam_plugin_sdk/resources";
  generated.push([path.join(root, resource, "runtime-contract.json"),
    readFileSync(path.join(root, "resources/logging/runtime-contract.json"), "utf8")]);
  for (const [file, content] of generated) {
    if (existsSync(file) && readFileSync(file, "utf8") === content) continue;
    if (check) throw new Error(`SDK 配置或生成资源过期: ${path.relative(root, file)}；先运行 prepare-product-sdk.mjs ${language}`);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, content);
  }
}
