import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { parse } from "jsonc-parser";
import { buildPluginSdk } from "../../plugins/build-sdk.mjs";
import { pluginDirectoryDigest } from "../../plugins/artifact-directory.mjs";
import { PACKAGE_FILES, buildDeveloperToolsDirectory } from "../../plugins/pack-developer-tools.mjs";

/** 开发与发行共用完整目录构建；Gateway 安装不补装依赖。 */
export async function prepareDevelopmentPlugins({ projectRoot, pythonExecutable, nodeExecutable, bunExecutable }) {
  if (!nodeExecutable) throw new Error("构建内置插件需要可用的 Node 可执行文件");
  const probe = Bun.spawnSync([nodeExecutable, "-p", "JSON.stringify({version:process.versions.node,abi:process.versions.modules,glibc:process.report.getReport().header.glibcVersionRuntime})"], { stdout: "pipe", stderr: "pipe" });
  if (probe.exitCode !== 0) throw new Error(`内置插件 Node 探测失败: ${probe.stderr.toString()}`);
  const runtime = JSON.parse(probe.stdout.toString());
  const baseline = Bun.spawnSync([pythonExecutable, "-c", "import json,platform,sys; print(json.dumps('glibc-'+platform.libc_ver()[1] if sys.platform=='linux' else 'windows-'+platform.version() if sys.platform=='win32' else 'macos-'+platform.mac_ver()[0]))"], { stdout: "pipe", stderr: "pipe" });
  if (baseline.exitCode !== 0) throw new Error(`内置插件系统基线探测失败: ${baseline.stderr.toString()}`);
  const target = { os: { linux: "linux", darwin: "macos", win32: "windows" }[process.platform],
    architecture: process.arch, system_baseline: JSON.parse(baseline.stdout.toString()),
    node: { version: runtime.version, abi: runtime.abi } };
  const sdk = buildPluginSdk({ projectRoot, language: "node", bunExecutable, nodeExecutable, development: true });
  const hash = createHash("sha256").update(JSON.stringify({ target, sdk: sdk.digest }));
  const bunVersion = Bun.spawnSync([bunExecutable, "--version"], { stdout: "pipe", stderr: "pipe" });
  if (bunVersion.exitCode !== 0) throw new Error(`内置插件 Bun 探测失败: ${bunVersion.stderr.toString()}`);
  hash.update(bunVersion.stdout);
  for (const file of [...PACKAGE_FILES.map(([source]) => source), "node_modules/node-gyp/package.json", "scripts/plugins/pack-developer-tools.mjs", "scripts/plugins/build-sdk.mjs", "scripts/plugins/artifact-directory.mjs", "scripts/launch/development/bundled-plugins.mjs"]) {
    hash.update(file).update(readFileSync(path.join(projectRoot, file)));
  }
  const directory = path.join(projectRoot, "out/development-runtime/bundled-plugins", hash.digest("hex"));
  const packageDirectory = path.join(directory, "developer-tools");
  if (!existsSync(packageDirectory)) {
    mkdirSync(directory, { recursive: true });
    const temporary = path.join(directory, `developer-tools-${process.pid}`);
    console.log("[dev] 构建内置开发工具完整目录（未变依赖复用）");
    try {
      await buildDeveloperToolsDirectory({ projectRoot, output: temporary, target, sdk, bunExecutable, nodeExecutable,
        pythonExecutable, nodeGypEntrypoint: path.join(projectRoot, "node_modules/node-gyp/bin/node-gyp.js") });
      const manifestPath = path.join(temporary, "plugin.jsonc");
      const manifest = parse(readFileSync(manifestPath, "utf8"));
      const digest = pluginDirectoryDigest(temporary);
      manifest.version = `${manifest.version}-dev.${digest}`;
      writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
      renameSync(temporary, packageDirectory);
    } finally { rmSync(temporary, { recursive: true, force: true }); }
  }
  const manifest = parse(readFileSync(path.join(packageDirectory, "plugin.jsonc"), "utf8"));
  return [{ package_id: manifest.package_id, package_version: manifest.version, directory: packageDirectory }];
}
