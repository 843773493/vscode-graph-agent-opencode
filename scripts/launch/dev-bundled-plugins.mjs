import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync } from "node:fs";
import path from "node:path";
import { parse } from "jsonc-parser";
import { PACKAGE_FILES, packDeveloperTools } from "../plugins/pack-developer-tools.mjs";

/** 发布准备归开发启动器；Gateway 只安装已构建的标准 ZIP。 */
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
  const hash = createHash("sha256").update(JSON.stringify(target));
  for (const file of [...PACKAGE_FILES.map(([source]) => source), "scripts/plugins/pack-developer-tools.mjs"]) {
    hash.update(file).update(readFileSync(path.join(projectRoot, file)));
  }
  const directory = path.join(projectRoot, "out/development-runtime/bundled-plugins", hash.digest("hex"));
  const archive = path.join(directory, "developer-tools.zip");
  if (!existsSync(archive)) {
    mkdirSync(directory, { recursive: true });
    const temporary = path.join(directory, `developer-tools-${process.pid}.zip`);
    console.log("[dev] 构建内置开发工具插件包（后续启动复用未变化的制品）");
    await packDeveloperTools({ projectRoot, output: temporary, target, bunExecutable, nodeExecutable,
      pythonExecutable, nodeGypEntrypoint: path.join(projectRoot, "node_modules/node-gyp/bin/node-gyp.js") });
    renameSync(temporary, archive);
  }
  const manifest = parse(readFileSync(path.join(projectRoot, "resources/plugins/developer-tools/plugin.jsonc"), "utf8"));
  return [{ package_id: manifest.package_id, package_version: manifest.version, archive }];
}
