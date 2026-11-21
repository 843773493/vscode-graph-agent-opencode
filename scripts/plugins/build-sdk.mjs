import { createHash } from "node:crypto";
import { chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
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

export function buildPluginSdk({ projectRoot, language, outputRoot = path.join(projectRoot, "out/sdk"), uvExecutable = "uv", bunExecutable = process.execPath }) {
  if (!["node", "python"].includes(language)) throw new Error(`未知 SDK 语言: ${language}`);
  projectRoot = path.resolve(projectRoot);
  const version = JSON.parse(readFileSync(path.join(projectRoot, "package.json"), "utf8")).version;
  mkdirSync(outputRoot, { recursive: true });
  const input = createHash("sha256").update(version).update(language).update(process.versions.bun ?? "");
  const include = file => !["AGENTS.md", "__pycache__", "node_modules", "build", "dist", "record-samples.json"].includes(path.basename(file))
    && !file.endsWith(".egg-info") && !file.endsWith(".pyc") && !file.endsWith(".test.mjs");
  function hashDirectory(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
      const file = path.join(directory, entry.name);
      if (!include(file)) continue;
      input.update(path.relative(projectRoot, file)).update("\0");
      if (entry.isDirectory()) hashDirectory(file);
      else input.update(readFileSync(file));
    }
  }
  hashDirectory(path.join(projectRoot, "sdk", language));
  input.update(readFileSync(path.join(projectRoot, "scripts/plugins/build-sdk.mjs")));
  input.update(readFileSync(path.join(projectRoot, "resources/logging/runtime-contract.json")));
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
    const source = path.join(stage, "source");
    cpSync(path.join(projectRoot, "sdk", language), source, { recursive: true,
      filter: include });
    const resourceRoot = path.join(source, "src", language === "python" ? "boxteam_plugin_sdk/resources" : "resources/logging");
    mkdirSync(resourceRoot, { recursive: true });
    cpSync(path.join(projectRoot, "resources/logging/runtime-contract.json"), path.join(resourceRoot, "runtime-contract.json"));
    // SDK 没有可执行文件；制品权限不继承 checkout 或构建进程的 umask。
    normalizePermissions(source);
    if (language === "node") {
      const manifestPath = path.join(source, "package.json");
      const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
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
