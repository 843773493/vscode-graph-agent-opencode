import { createWriteStream, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { finished } from "node:stream/promises";
import { pathToFileURL } from "node:url";
import yazl from "yazl";
import { parse as parseJsonc } from "jsonc-parser";

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

function collectFiles(root, directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    if (entry.name === "AGENTS.md" || entry.name === "__pycache__" || entry.name === ".DS_Store") return [];
    const absolute = path.join(directory, entry.name);
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    if (entry.isSymbolicLink()) throw new Error(`MCP 插件制品不能包含符号链接: ${relative}`);
    return entry.isDirectory()
      ? collectFiles(root, absolute)
      : [[absolute, relative]];
  });
}

async function writeArchive(output, files) {
  const archive = new yazl.ZipFile();
  for (const [source, archivePath] of files) archive.addFile(source, archivePath, { compress: true });
  const stream = createWriteStream(output, { flags: "wx" });
  const closed = finished(stream);
  archive.outputStream.pipe(stream);
  archive.end();
  await closed;
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

export async function packMcpStdio({ projectRoot, output, target, uvExecutable, pythonExecutable }) {
  projectRoot = path.resolve(projectRoot);
  output = path.resolve(output);
  if (!path.isAbsolute(output) || !output.toLowerCase().endsWith(".zip")) throw new Error("MCP 插件包输出必须是明确的 ZIP 绝对路径");
  if (existsSync(output)) throw new Error(`拒绝覆盖已有插件包: ${output}`);
  const packageRoot = path.join(projectRoot, "resources/plugins/mcp-stdio");
  for (const required of ["plugin.jsonc", "bridge.py", "pyproject.toml", "uv.lock"]) {
    if (!existsSync(path.join(packageRoot, required))) throw new Error(`MCP stdio 插件缺少 ${required}`);
  }
  const manifestErrors = [];
  const manifest = parseJsonc(readFileSync(path.join(packageRoot, "plugin.jsonc"), "utf8"), manifestErrors);
  if (manifestErrors.length || manifest.package_id !== "boxteam.mcp-stdio" || manifest.plugins.length !== 1) {
    throw new Error("MCP stdio 插件 manifest 无效");
  }
  const targetManifest = validateTarget(target, pythonExecutable);
  const stage = mkdtempSync(path.join(os.tmpdir(), "boxteam-mcp-stdio-"));
  const credentialHome = path.join(stage, "home");
  const requirementsFile = path.join(stage, "requirements.txt");
  const sitePackages = path.join(stage, "python/site-packages");
  mkdirSync(credentialHome);
  mkdirSync(sitePackages, { recursive: true });
  const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(UV_|PIP_|PYTHON)/i.test(key)));
  environment.HOME = credentialHome;
  environment.USERPROFILE = credentialHome;
  environment.UV_CACHE_DIR = process.env.UV_CACHE_DIR ?? (process.platform === "win32"
    ? path.join(process.env.LOCALAPPDATA ?? os.homedir(), "uv/cache")
    : path.join(os.homedir(), ".cache/uv"));
  const manifestPath = path.join(stage, "plugin.jsonc");
  try {
    await Bun.write(manifestPath, `${JSON.stringify({ ...manifest, target: targetManifest }, null, 2)}\n`);
    run(uvExecutable, ["--no-config", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
      "--project", packageRoot, "--output-file", requirementsFile], packageRoot, environment);
    run(uvExecutable, ["--no-config", "pip", "install", "--python", pythonExecutable, "--target", sitePackages,
      "--require-hashes", "--no-deps", "--no-build", "--requirements", requirementsFile], packageRoot, environment);
    const files = collectFiles(packageRoot, packageRoot).filter(([source]) => !source.endsWith("/plugin.jsonc"));
    files.push(...collectFiles(stage, sitePackages));
    files.push([manifestPath, "plugin.jsonc"]);
    for (const source of ["app/__init__.py", "app/core/__init__.py", "app/core/runtime_observation.py",
      "app/schemas/__init__.py", "app/schemas/runtime_observation.py",
      "app/runtime/plugin_observation.py"]) {
      files.push([path.join(projectRoot, source), source]);
    }
    await writeArchive(output, files);
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const projectRoot = process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd();
  const options = parseArguments(process.argv.slice(2));
  await packMcpStdio({ projectRoot, ...options });
  console.log(`MCP stdio 插件 ZIP 已生成: ${options.output}`);
}
