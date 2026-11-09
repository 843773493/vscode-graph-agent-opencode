import { createWriteStream, existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { finished } from "node:stream/promises";
import { pathToFileURL } from "node:url";
import yazl from "yazl";
import { parse as parseJsonc } from "jsonc-parser";

const SOURCE_FILES = [
  "src/workspace-services/debug/server/backend.js",
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
];

const PACKAGE_FILES = [
  ["resources/plugins/developer-tools/skills/debugging/SKILL.md", "skills/debugging/SKILL.md"],
  ["resources/plugins/developer-tools/plugin.jsonc", "plugin.jsonc"],
  ["resources/plugins/developer-tools/package.json", "package.json"],
  ["resources/plugins/developer-tools/bun.lock", "bun.lock"],
  ...SOURCE_FILES.map((file) => [file, file]),
];

function parseOutputPath(args) {
  if (args.length !== 2 || args[0] !== "--out" || !args[1]) {
    throw new Error("用法: bun scripts/plugins/pack-developer-tools.mjs --out <zip绝对路径>");
  }
  const output = path.resolve(process.cwd(), args[1]);
  if (!output.toLowerCase().endsWith(".zip")) throw new Error("--out 必须指向 .zip 文件");
  return output;
}

function assertRepositoryRoot(root) {
  for (const required of ["package.json", "src/workspace-services/terminal/server/backend.js", "resources"]) {
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

export async function packDeveloperTools(projectRoot, output) {
  assertRepositoryRoot(projectRoot);
  if (!path.isAbsolute(output) || !output.endsWith(".zip")) throw new Error("插件包输出必须是明确的 ZIP 绝对路径");
  if (existsSync(output)) throw new Error(`拒绝覆盖已有插件包: ${output}`);
  packageDependencies(projectRoot);
  for (const [source] of PACKAGE_FILES) {
    if (!existsSync(path.join(projectRoot, source))) throw new Error(`插件包清单文件缺失: ${source}`);
  }
  await writeArchive(output, PACKAGE_FILES.map(([source, archivePath]) => [path.join(projectRoot, source), archivePath]));
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const output = parseOutputPath(process.argv.slice(2));
  await packDeveloperTools(process.cwd(), output);
  console.log(`插件 ZIP 已生成: ${output} (${PACKAGE_FILES.length} 个文件)`);
}
