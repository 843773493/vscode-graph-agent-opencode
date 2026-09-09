import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { BOXTEAM_VERSION } from "../../packaging/runtime/versions.mjs";

const RELEASE_PACKAGE_NAME = "boxteam";

function requiredValue(value, optionName) {
  const normalized = value?.trim() ?? "";
  if (normalized === "" || normalized.startsWith("-")) {
    throw new Error(`${optionName} 必须提供非空值`);
  }
  return normalized;
}

export function buildReleaseInstallCommand({
  prefix = null,
  platform = process.platform,
} = {}) {
  const args = ["install", "--global", "--no-audit", "--no-fund"];
  if (prefix !== null) {
    args.push("--prefix", path.resolve(requiredValue(prefix, "--prefix")));
  }
  args.push(`${RELEASE_PACKAGE_NAME}@${BOXTEAM_VERSION}`);
  return Object.freeze({
    command: platform === "win32" ? "npm.cmd" : "npm",
    args: Object.freeze(args),
    packageSpec: `${RELEASE_PACKAGE_NAME}@${BOXTEAM_VERSION}`,
  });
}

export function parseInstallArguments(args) {
  let prefix = null;
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "--prefix") {
      prefix = requiredValue(args[++index], "--prefix");
    } else if (argument === "--help" || argument === "-h") {
      if (args.length !== 1) {
        throw new Error("--help 不能与其他参数同时使用");
      }
      return Object.freeze({ help: true });
    } else {
      throw new Error(`未知发布安装参数: ${argument}`);
    }
  }
  return Object.freeze({ help: false, prefix });
}

export function runReleaseInstall({
  command,
  args,
  cwd = process.cwd(),
  environment = process.env,
  spawnSyncImpl = spawnSync,
} = {}) {
  const result = spawnSyncImpl(command, args, {
    cwd,
    env: environment,
    stdio: "inherit",
  });
  if (result.error) {
    throw new Error(`执行发布版安装失败: ${command}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(
      `发布版安装失败: ${command} ${args.join(" ")} exit=${String(result.status)}`,
    );
  }
}

export function main(args = process.argv.slice(2)) {
  const options = parseInstallArguments(args);
  if (options.help) {
    process.stdout.write(
      "用法: bun run scripts/install/install-release.mjs [--prefix <目录>]\n" +
        "安装当前项目发布版本的 BoxTeam CLI 和平台 runtime。\n" +
        "安装完成后使用 boxteam start 启动 packaged Gateway。\n",
    );
    return;
  }
  const command = buildReleaseInstallCommand({ prefix: options.prefix });
  runReleaseInstall(command);
  process.stdout.write(
    `发布版安装完成: ${command.packageSpec}\n` +
      "下一步: boxteam start（Gateway 将启动内置 Workspace 后端并托管打包 Web UI）\n",
  );
}

const currentModulePath = fileURLToPath(import.meta.url);
if (process.argv[1] && path.resolve(process.argv[1]) === currentModulePath) {
  main();
}
