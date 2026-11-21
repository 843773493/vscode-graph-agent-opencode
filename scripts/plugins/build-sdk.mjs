import { createHash } from "node:crypto";
import { chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

function run(command, args, cwd) {
  const result = Bun.spawnSync([command, ...args], { cwd, env: { ...process.env, SOURCE_DATE_EPOCH: "315532800" }, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) throw new Error(`SDK 构建失败: ${command} ${args.join(" ")}\n${result.stderr.toString()}`);
  return result.stdout.toString();
}

function normalizePermissions(directory) {
  chmodSync(directory, 0o755);
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) normalizePermissions(file);
    else chmodSync(file, 0o644);
  }
}

export function buildPluginSdk({ projectRoot, language, outputRoot = path.join(projectRoot, "out/sdk"), uvExecutable = "uv", bunExecutable = process.execPath, nodeExecutable = "node", development = false }) {
  if (!["node", "python"].includes(language)) throw new Error(`未知 SDK 语言: ${language}`);
  projectRoot = path.resolve(projectRoot);
  const version = JSON.parse(readFileSync(path.join(projectRoot, "package.json"), "utf8")).version;
  mkdirSync(outputRoot, { recursive: true });
  const input = createHash("sha256").update(version).update(language).update(process.versions.bun ?? "");
  if (language === "node") input.update(development ? "runtime-only" : "declarations").update(readFileSync(path.join(projectRoot, "bun.lock")));
  const include = file => !["AGENTS.md", "__pycache__", "node_modules", "build", "dist", "record-samples.json"].includes(path.basename(file))
    && !file.endsWith(".egg-info") && !file.endsWith(".pyc") && !file.endsWith(".test.mjs");
  const inputs = new Map();
  const sdkRoot = path.join(projectRoot, "sdk", language);
  function readDirectory(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
      const file = path.join(directory, entry.name);
      if (!include(file)) continue;
      if (entry.isDirectory()) readDirectory(file);
      else inputs.set(path.relative(sdkRoot, file), readFileSync(file));
    }
  }
  readDirectory(sdkRoot);
  const resource = path.join("src", language === "python" ? "boxteam_plugin_sdk/resources" : "resources/logging", "runtime-contract.json");
  inputs.set(resource, readFileSync(path.join(projectRoot, "resources/logging/runtime-contract.json")));
  for (const [file, content] of [...inputs].sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)) {
    input.update(file).update("\0").update(content);
  }
  input.update(readFileSync(path.join(projectRoot, "scripts/plugins/build-sdk.mjs")));
  const tool = language === "python" ? uvExecutable : bunExecutable;
  input.update(run(tool, ["--version"], projectRoot));
  const cache = path.join(outputRoot, "inputs", language, input.digest("hex"));
  function publish(archive) {
    const digest = createHash("sha256").update(readFileSync(archive)).digest("hex");
    const destination = path.join(outputRoot, language, digest, path.basename(archive));
    mkdirSync(path.dirname(destination), { recursive: true });
    if (!existsSync(destination)) cpSync(archive, destination, { errorOnExist: true, force: false });
    return { language, version, digest, archive: destination };
  }
  if (existsSync(cache)) {
    const archives = readdirSync(cache);
    if (archives.length !== 1) throw new Error(`SDK 构建缓存不完整: ${cache}`);
    return publish(path.join(cache, archives[0]));
  }
  const stage = mkdtempSync(path.join(outputRoot, `.${language}-`));
  try {
    const source = path.join(stage, "sdk", language);
    for (const [file, content] of inputs) {
      const destination = path.join(source, file);
      mkdirSync(path.dirname(destination), { recursive: true });
      writeFileSync(destination, content);
    }
    // SDK 没有可执行文件；制品权限不继承 checkout 或构建进程的 umask。
    normalizePermissions(source);
    if (language === "node") {
      // 构建只读取冻结输入；临时依赖链接用于标准解析，归档前移除。
      symlinkSync(path.join(sdkRoot, "node_modules"), path.join(source, "node_modules"), process.platform === "win32" ? "junction" : "dir");
      run(nodeExecutable, [path.join(source, "build.mjs"), ...(development ? [] : ["--release"])], stage);
      rmSync(path.join(source, "node_modules"));
      normalizePermissions(path.join(source, "dist"));
      const manifestPath = path.join(source, "package.json");
      const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
      for (const entry of Object.values(manifest.exports)) {
        if (typeof entry !== "object") continue;
        if (development) delete entry.types;
        else entry.types = entry.types.replace("./src/", "./dist/").replace(/\.ts$/, ".d.ts");
      }
      writeFileSync(manifestPath, JSON.stringify({ ...manifest, version }, null, 2) + "\n");
      run(bunExecutable, ["pm", "pack", "--ignore-scripts", "--destination", stage], source);
    } else {
      const manifestPath = path.join(source, "pyproject.toml");
      writeFileSync(manifestPath, readFileSync(manifestPath, "utf8").replace(/^version = .*$/m, `version = "${version}"`));
      run(uvExecutable, ["build", "--wheel", "--out-dir", stage, source], projectRoot);
    }
    const archives = readdirSync(stage).filter((name) => name.endsWith(language === "python" ? ".whl" : ".tgz"));
    if (archives.length !== 1) throw new Error(`SDK 构建未生成唯一归档: ${archives}`);
    const archive = path.join(stage, archives[0]);
    const result = publish(archive);
    mkdirSync(path.dirname(cache), { recursive: true });
    const prepared = path.join(stage, "cache");
    mkdirSync(prepared);
    renameSync(archive, path.join(prepared, archives[0]));
    renameSync(prepared, cache);
    return result;
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const language = process.argv[2];
  console.log(JSON.stringify(buildPluginSdk({ projectRoot: process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(), language })));
}
