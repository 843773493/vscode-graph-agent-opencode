import { cpSync, existsSync, mkdtempSync, mkdirSync, readFileSync, renameSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse as parseJsonc } from "jsonc-parser";
import { createHash } from "node:crypto";
import { buildPluginSdk } from "./build-sdk.mjs";
import { archivePluginDirectory } from "./artifact-directory.mjs";

function parseArguments(args) {
  const values = new Map();
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (!key?.startsWith("--") || !value || values.has(key)) throw new Error("MCP 插件打包参数必须是唯一的 --key value 对");
    values.set(key, value);
  }
  for (const key of ["--out", "--target-json", "--python", "--uv"]) {
    if (!values.has(key)) throw new Error(`MCP 插件打包缺少 ${key}`);
  }
  const output = path.resolve(values.get("--out"));
  if (!path.isAbsolute(output) || !output.toLowerCase().endsWith(".zip")) throw new Error("--out 必须是绝对 ZIP 路径");
  return {
    output,
    target: JSON.parse(values.get("--target-json")),
    pythonExecutable: path.resolve(values.get("--python")),
    uvExecutable: path.resolve(values.get("--uv")),
  };
}

function run(executable, args, cwd, env) {
  const result = Bun.spawnSync([executable, ...args], { cwd, env, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) {
    throw new Error(`MCP 插件构建阶段失败: ${path.basename(executable)} ${args[0]} exit=${result.exitCode}: ${result.stderr.toString("utf8")}`);
  }
  return result.stdout.toString("utf8");
}

function validateTarget(target, pythonExecutable) {
  const hostOs = { linux: "linux", win32: "windows", darwin: "macos" }[process.platform];
  const hostArchitecture = { x64: "x64", arm64: "arm64" }[process.arch];
  if (target?.os !== hostOs || target?.architecture !== hostArchitecture) {
    throw new Error("MCP 插件含 Python 目标依赖，当前构建主机没有匹配的原生制品；拒绝跨 OS/架构构建");
  }
  if (!target.python) throw new Error("MCP stdio 插件 target 必须声明 Python runtime");
  const actual = JSON.parse(Bun.spawnSync([pythonExecutable, "-c",
    "import json,sys; print(json.dumps({'version':sys.version.split()[0], 'abi':f'cp{sys.version_info.major}{sys.version_info.minor}'}))"],
    { stdout: "pipe", stderr: "pipe" }).stdout.toString("utf8"));
  if (actual.version !== target.python.version || actual.abi !== target.python.abi) {
    throw new Error(`MCP 插件 Python 目标与构建解释器不匹配: target=${target.python.version}/${target.python.abi}, actual=${actual.version}/${actual.abi}`);
  }
  const manifestTarget = { os: target.os, architecture: target.architecture,
    system_baseline: target.system_baseline, python: target.python };
  return manifestTarget;
}

export async function buildMcpStdioDirectory(options) {
  const { projectRoot, output, target, uvExecutable, pythonExecutable } = options;
  if (!path.isAbsolute(output) || existsSync(output)) throw new Error(`MCP 插件目录必须是未使用的绝对路径: ${output}`);
  const packageRoot = path.join(projectRoot, "resources/plugins/mcp-stdio");
  const manifest = parseJsonc(readFileSync(path.join(packageRoot, "plugin.jsonc"), "utf8"));
  const targetManifest = validateTarget(target, pythonExecutable);
  const sdk = options.sdk ?? buildPluginSdk({ projectRoot, language: "python", uvExecutable });
  const hash = createHash("sha256").update(JSON.stringify({ target, sdk: sdk.digest, uvExecutable, pythonExecutable }));
  hash.update(run(uvExecutable, ["--version"], projectRoot, process.env));
  for (const file of ["resources/plugins/mcp-stdio/pyproject.toml", "resources/plugins/mcp-stdio/uv.lock", "scripts/plugins/pack-mcp-stdio.mjs"]) {
    hash.update(readFileSync(path.join(projectRoot, file)));
  }
  const cache = path.join(projectRoot, "out/plugin-dependencies/mcp-stdio", hash.digest("hex"));
  mkdirSync(path.dirname(cache), { recursive: true });
  if (!existsSync(cache)) {
    const stage = mkdtempSync(path.join(path.dirname(cache), ".prepare-"));
    try {
      const source = path.join(stage, "package");
      mkdirSync(source);
      const sdkPath = `vendor/sdk/${sdk.digest}/${path.basename(sdk.archive)}`;
      mkdirSync(path.dirname(path.join(source, sdkPath)), { recursive: true });
      cpSync(sdk.archive, path.join(source, sdkPath));
      const project = readFileSync(path.join(packageRoot, "pyproject.toml"), "utf8")
        .replace("dependencies = [", `dependencies = [\n    "boxteam-plugin-sdk==${sdk.version}",`)
        + `\n[tool.uv.sources]\nboxteam-plugin-sdk = { path = "${sdkPath}" }\n`;
      await Bun.write(path.join(source, "pyproject.toml"), project);
      cpSync(path.join(packageRoot, "uv.lock"), path.join(source, "uv.lock"));
      const home = path.join(stage, "home");
      mkdirSync(home);
      const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(UV_|PIP_|PYTHON)/i.test(key)));
      environment.HOME = home;
      environment.USERPROFILE = home;
      environment.UV_CACHE_DIR = process.env.UV_CACHE_DIR ?? (process.platform === "win32"
        ? path.join(process.env.LOCALAPPDATA ?? os.homedir(), "uv/cache") : path.join(os.homedir(), ".cache/uv"));
      const requirements = path.join(stage, "requirements.txt");
      run(uvExecutable, ["--no-config", "lock", "--project", source, "--python", pythonExecutable], source, environment);
      run(uvExecutable, ["--no-config", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
        "--project", source, "--output-file", requirements], source, environment);
      run(uvExecutable, ["--no-config", "pip", "install", "--python", pythonExecutable, "--target", path.join(source, "python/site-packages"),
        "--require-hashes", "--no-deps", "--no-build", "--requirements", requirements], source, environment);
      renameSync(source, cache);
    } finally { rmSync(stage, { recursive: true, force: true }); }
  }
  mkdirSync(path.dirname(output), { recursive: true });
  const stage = mkdtempSync(path.join(path.dirname(output), ".mcp-stdio-"));
  try {
    const directory = path.join(stage, "package");
    cpSync(cache, directory, { recursive: true });
    cpSync(path.join(packageRoot, "bridge.py"), path.join(directory, "bridge.py"));
    await Bun.write(path.join(directory, "plugin.jsonc"), JSON.stringify({ ...manifest, target: targetManifest }, null, 2) + "\n");
    renameSync(directory, output);
    return output;
  } finally { rmSync(stage, { recursive: true, force: true }); }
}

export async function packMcpStdio(options) {
  const { output } = options;
  if (!path.isAbsolute(output) || !output.endsWith(".zip") || existsSync(output)) throw new Error(`MCP 插件包输出必须是未使用的 ZIP 绝对路径: ${output}`);
  mkdirSync(path.dirname(output), { recursive: true });
  const stage = mkdtempSync(path.join(path.dirname(output), ".mcp-stdio-zip-"));
  try {
    const directory = await buildMcpStdioDirectory({ ...options, output: path.join(stage, "package") });
    await archivePluginDirectory(directory, output);
  } finally { rmSync(stage, { recursive: true, force: true }); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const projectRoot = process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd();
  const options = parseArguments(process.argv.slice(2));
  await packMcpStdio({ projectRoot, ...options });
  console.log(`MCP stdio 插件 ZIP 已生成: ${options.output}`);
}
