import { createWriteStream, cpSync, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { finished } from "node:stream/promises";
import { pathToFileURL } from "node:url";
import yazl from "yazl";
import { parse as parseJsonc } from "jsonc-parser";

const SOURCE_FILES = [
  "src/shared/serverHttpShutdown.mjs",
  "src/shared/serverJsonRequest.mjs",
  "src/shared/serverPluginRuntime.mjs",
  "src/shared/serverPluginCommands.mjs",
  "src/shared/serverRuntimeControl.mjs",
  "src/shared/serverRuntimeLogging.mjs",
  "src/workspace-services/protocol/generated/boxteam/common/v1/error_pb.js",
  "src/workspace-services/protocol/generated/boxteam/gateway/v2/service_plugin_pb.js",
  "src/workspace-services/protocol/generated/boxteam/gateway/v2/plugin_commands_pb.js",
  "src/workspace-services/protocol/generated/boxteam/terminal/v1/terminal_input_pb.js",
  "src/workspace-services/protocol/generated/boxteam/terminal/v1/terminal_output_pb.js",
  "src/workspace-services/protocol/generated/boxteam/terminal/v1/terminal_pb.js",
  "src/workspace-services/terminal/protocol/messages.js",
  "src/workspace-services/terminal/server/backend.js",
  "src/workspace-services/terminal/server/output/terminalOutputMultiplexer.js",
  "src/workspace-services/terminal/server/output/terminalUtf8Output.js",
  "src/workspace-services/terminal/server/processes/isolatedPtyProcess.js",
  "src/workspace-services/terminal/server/processes/ptyWorker.js",
  "src/shared/serverProcessLifecycle.mjs",
  "src/workspace-services/terminal/server/terminalInput.js",
  "src/workspace-services/terminal/server/terminalManager.js",
  "src/workspace-services/terminal/server/terminalSession.js",
  "src/workspace-services/terminal/server/terminalStateStore.js",
  "src/workspace-services/terminal/server/workspaceIdentity.js",
  "src/workspace-services/terminal/shared/commandText.js",
  "src/workspace-services/browser/protocol/messages.js",
  "src/workspace-services/browser/server/backend.js",
  "src/workspace-services/browser/server/browserDeviceProfiles.js",
  "src/workspace-services/browser/server/browserManager.js",
  "src/workspace-services/browser/server/browserRuntime.js",
  "src/workspace-services/browser/server/browserSession.js",
  "src/workspace-services/browser/server/browserStateStore.js",
  "src/workspace-services/browser/server/diagnostics/pageDiagnostics.js",
  "src/workspace-services/browser/server/diagnostics/streamMetrics.js",
  "src/workspace-services/browser/server/operations/browserInput.js",
  "src/workspace-services/browser/server/operations/browserOperationQueue.js",
  "src/workspace-services/browser/server/operations/browserPageActions.js",
  "src/workspace-services/browser/server/operations/latestPointerMoveDispatcher.js",
  "src/workspace-services/browser/server/resources/browserCheckpoint.js",
  "src/workspace-services/browser/server/resources/browserRuntimePool.js",
  "src/workspace-services/browser/server/resources/memoryPressure.js",
  "src/workspace-services/browser/server/resources/resourceGovernor.js",
  "src/workspace-services/browser/server/resources/resourcePolicy.js",
  "src/workspace-services/browser/server/transport/browserFrameFlow.js",
  "src/workspace-services/browser/server/transport/protocol.js",
  "src/workspace-services/browser/server/url.js",
  "src/workspace-services/browser/server/workspaceIdentity.js",
  "src/workspace-services/browser/client/index.html",
  "src/workspace-services/browser/client/styles.css",
  "src/workspace-services/browser/client/main.js",
  "src/workspace-services/browser/client/browserClientUtils.js",
  "src/workspace-services/browser/client/browserCollaborationUi.js",
  "src/workspace-services/browser/client/browserDebugElementTree.js",
  "src/workspace-services/browser/client/browserElementContext.js",
  "src/workspace-services/browser/client/browserInputEvents.js",
  "src/workspace-services/browser/client/browserModalUi.js",
  "src/workspace-services/browser/client/browserShortcuts.js",
  "src/workspace-services/browser/client/browserToolbarEvents.js",
  "src/workspace-services/protocol/generated/boxteam/browser/v1/browser_input_pb.js",
  "src/workspace-services/protocol/generated/boxteam/browser/v1/browser_page_pb.js",
  "src/workspace-services/protocol/generated/boxteam/browser/v1/browser_pb.js",
  "src/workspace-services/debug/server/backend.js",
];

const PACKAGE_FILES = [
  ["resources/plugins/developer-tools/plugin.jsonc", "plugin.jsonc"],
  ["resources/plugins/developer-tools/package.json", "package.json"],
  ["resources/plugins/developer-tools/bun.lock", "bun.lock"],
  ["resources/plugins/developer-tools/skills/browser-control/SKILL.md", "skills/browser-control/SKILL.md"],
  ["resources/plugins/developer-tools/skills/debugging/SKILL.md", "skills/debugging/SKILL.md"],
  ...SOURCE_FILES.map((file) => [file, file]),
];

function parseArguments(args) {
  const values = new Map();
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index], value = args[index + 1];
    if (!key?.startsWith("--") || !value || values.has(key)) throw new Error("插件打包参数必须是唯一的 --key value 对");
    values.set(key, value);
  }
  for (const key of ["--out", "--target-json", "--bun", "--node", "--node-gyp", "--python"]) {
    if (!values.has(key)) throw new Error(`developer-tools 打包缺少 ${key}`);
  }
  const output = path.resolve(values.get("--out"));
  if (!output.toLowerCase().endsWith(".zip")) throw new Error("--out 必须指向 .zip 文件");
  return { output, target: JSON.parse(values.get("--target-json")),
    bunExecutable: path.resolve(values.get("--bun")), nodeExecutable: path.resolve(values.get("--node")),
    nodeGypEntrypoint: path.resolve(values.get("--node-gyp")), pythonExecutable: path.resolve(values.get("--python")) };
}

function assertRepositoryRoot(root) {
  for (const required of ["package.json", "src/workspace-services/terminal/server/backend.js", "src/workspace-services/browser/server/backend.js", "src/workspace-services/debug/server/backend.js", "resources"]) {
    if (!existsSync(path.join(root, required))) {
      throw new Error(`请从仓库根目录运行插件打包脚本；缺少 ${required}`);
    }
  }
}

function packageDependencies(root) {
  const manifest = JSON.parse(readFileSync(path.join(root, "resources/plugins/developer-tools/package.json"), "utf8"));
  const errors = [];
  const lock = parseJsonc(readFileSync(path.join(root, "resources/plugins/developer-tools/bun.lock"), "utf8"), errors, { allowTrailingComma: true });
  if (errors.length) throw new Error("developer-tools bun.lock 不是合法的 Bun 文本锁文件");
  if (lock.lockfileVersion !== 1 || JSON.stringify(manifest.dependencies) !== JSON.stringify(lock.workspaces?.[""]?.dependencies)) {
    throw new Error("developer-tools package.json 与 Bun 文本锁文件 根依赖不一致");
  }
  return new Set(Object.keys(manifest.dependencies));
}

async function writeArchive(output, files) {
  const archive = new yazl.ZipFile();
  for (const [source, archivePath] of files) {
    archive.addFile(source, archivePath, { compress: true });
  }
  const stream = createWriteStream(output, { flags: "wx" });
  const closed = finished(stream);
  archive.outputStream.pipe(stream);
  archive.end();
  await closed;
}

function run(executable, args, cwd, env) {
  const result = Bun.spawnSync([executable, ...args], { cwd, env, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) {
    throw new Error(`插件构建阶段失败: ${path.basename(executable)} ${args[0]} exit=${result.exitCode}: ${result.stderr.toString("utf8")}`);
  }
}

function collectFiles(root, directory, archivePrefix) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(directory, entry.name);
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    if (entry.isSymbolicLink()) throw new Error(`插件制品不能包含符号链接: ${relative}`);
    return entry.isDirectory()
      ? collectFiles(root, absolute, archivePrefix)
      : [[absolute, path.posix.join(archivePrefix, relative)]];
  });
}

function prepareDependencies({ projectRoot, stage, target, bunExecutable, nodeExecutable, nodeGypEntrypoint, pythonExecutable }) {
  if (!target || target.os !== ({ linux: "linux", win32: "windows", darwin: "macos" }[process.platform]) ||
      target.architecture !== ({ x64: "x64", arm64: "arm64" }[process.arch])) {
    throw new Error("developer-tools 含 node-pty，当前构建主机没有匹配的原生目标制品；拒绝跨 OS/架构构建");
  }
  if (!bunExecutable || !nodeExecutable || !nodeGypEntrypoint) throw new Error("插件发布构建缺少 Bun、目标 Node 或 node-gyp");
  const probe = Bun.spawnSync([nodeExecutable, "-p", "JSON.stringify({version:process.versions.node,abi:process.versions.modules})"], { stdout: "pipe", stderr: "pipe" });
  if (probe.exitCode !== 0) throw new Error("目标 Node 不能在当前构建主机执行，缺少可验证的 node-pty 目标制品");
  const runtime = JSON.parse(probe.stdout.toString("utf8"));
  if (runtime.version !== target.node.version || runtime.abi !== target.node.abi) {
    throw new Error(`目标 Node 与插件 manifest 不匹配: ${runtime.version}/${runtime.abi}`);
  }
  const sourceRoot = path.join(projectRoot, "resources/plugins/developer-tools");
  const packageRoot = path.join(stage, "source");
  mkdirSync(packageRoot, { recursive: true });
  cpSync(path.join(sourceRoot, "package.json"), path.join(packageRoot, "package.json"));
  cpSync(path.join(sourceRoot, "bun.lock"), path.join(packageRoot, "bun.lock"));
  const credentialHome = path.join(stage, "home");
  mkdirSync(credentialHome);
  const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(NPM_|UV_|PIP_|PYTHON|BUN_)/i.test(key)));
  environment.HOME = credentialHome;
  environment.USERPROFILE = credentialHome;
  environment.BUN_INSTALL_CACHE_DIR = process.env.BUN_INSTALL_CACHE_DIR ?? path.join(os.homedir(), ".bun/install/cache");
  run(bunExecutable, ["--no-env-file", "install", "--frozen-lockfile", "--production", "--ignore-scripts"], packageRoot, environment);
  rmSync(path.join(packageRoot, "node_modules/.bin"), { recursive: true, force: true });
  const nativePackage = path.join(packageRoot, "node_modules/node-pty");
  run(nodeExecutable, [nodeGypEntrypoint, "rebuild", `--target=${runtime.version}`,
    ...(pythonExecutable ? [`--python=${pythonExecutable}`] : [])], nativePackage, environment);
  return packageRoot;
}

export async function packDeveloperTools({ projectRoot, output, target, bunExecutable, nodeExecutable, nodeGypEntrypoint, pythonExecutable }) {
  assertRepositoryRoot(projectRoot);
  if (!path.isAbsolute(output) || !output.endsWith(".zip")) throw new Error("插件包输出必须是明确的 ZIP 绝对路径");
  if (existsSync(output)) throw new Error(`拒绝覆盖已有插件包: ${output}`);
  packageDependencies(projectRoot);
  for (const [source] of PACKAGE_FILES) {
    if (!existsSync(path.join(projectRoot, source))) throw new Error(`插件包清单文件缺失: ${source}`);
  }
  const stage = mkdtempSync(path.join(os.tmpdir(), "boxteam-developer-tools-"));
  const packageRoot = path.join(projectRoot, "resources/plugins/developer-tools");
  try {
    const preparedRoot = prepareDependencies({ projectRoot, stage, target, bunExecutable, nodeExecutable, nodeGypEntrypoint, pythonExecutable });
    const manifestTarget = { os: target.os, architecture: target.architecture,
      system_baseline: target.system_baseline, node: target.node };
    const manifest = { ...parseJsonc(readFileSync(path.join(packageRoot, "plugin.jsonc"), "utf8")), target: manifestTarget };
    const manifestPath = path.join(stage, "plugin.jsonc");
    await Bun.write(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
    const files = PACKAGE_FILES.filter(([, archivePath]) => archivePath !== "plugin.jsonc")
      .map(([source, archivePath]) => [path.join(projectRoot, source), archivePath]);
    files.push(...collectFiles(preparedRoot, path.join(preparedRoot, "node_modules"), ""));
    files.push([manifestPath, "plugin.jsonc"]);
    await writeArchive(output, files);
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const options = parseArguments(process.argv.slice(2));
  await packDeveloperTools({ projectRoot: process.cwd(), ...options });
  console.log(`插件 ZIP 已生成: ${options.output}`);
}
