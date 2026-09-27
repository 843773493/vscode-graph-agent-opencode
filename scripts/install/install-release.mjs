import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { computeBuildInputFingerprint } from "../../packaging/runtime/build-input-fingerprint.mjs";
import { sha256File } from "../../packaging/runtime/runtime-release-assets.mjs";
import { BOXTEAM_VERSION } from "../../packaging/runtime/versions.mjs";

const LAUNCHER_PACKAGE_NAME = "boxteam";

function requiredValue(value, optionName) {
  const normalized = value?.trim() ?? "";
  if (normalized === "" || normalized.startsWith("-")) {
    throw new Error(`${optionName} 必须提供非空值`);
  }
  return normalized;
}

function resolveTargetPlatform(platform, architecture) {
  if (platform === "linux" && architecture === "x64") return "linux-x64";
  if (platform === "win32" && architecture === "x64") return "windows-x64";
  throw new Error(
    `本地发布安装仅支持 Linux x64 和 Windows x64，实际为 ${platform}-${architecture}`,
  );
}

function resolveLocalPackagePaths({ projectRoot, targetPlatform, version }) {
  const outputRoot = path.join(projectRoot, "out", "packaging", targetPlatform);
  return Object.freeze({
    buildResult: path.join(outputRoot, "build-result.json"),
    buildScript: path.join(
      projectRoot,
      "scripts",
      "release",
      `package-${targetPlatform}.mjs`,
    ),
    launcherTarball: path.join(
      outputRoot,
      "tarballs",
      `${LAUNCHER_PACKAGE_NAME}-${version}.tgz`,
    ),
    runtimeTarball: path.join(
      outputRoot,
      "release-assets",
      `boxteam-runtime-${targetPlatform}-${version}.tgz`,
    ),
  });
}

async function buildResultMatches({
  paths,
  targetPlatform,
  version,
  sourceFingerprint,
}) {
  if (
    !existsSync(paths.buildResult) ||
    !existsSync(paths.launcherTarball) ||
    !existsSync(paths.runtimeTarball)
  ) {
    return false;
  }

  const launcherStats = statSync(paths.launcherTarball);
  const runtimeStats = statSync(paths.runtimeTarball);
  if (
    !launcherStats.isFile() ||
    !runtimeStats.isFile() ||
    launcherStats.size === 0 ||
    runtimeStats.size === 0
  ) {
    return false;
  }

  let result;
  try {
    result = JSON.parse(readFileSync(paths.buildResult, "utf8"));
  } catch (error) {
    if (error instanceof SyntaxError) return false;
    throw error;
  }

  if (result === null || typeof result !== "object" || Array.isArray(result)) {
    return false;
  }
  const installTarballs = result.install_tarballs;
  if (
    !installTarballs ||
    typeof installTarballs !== "object" ||
    installTarballs.launcher?.filename !== path.basename(paths.launcherTarball) ||
    installTarballs.runtime?.filename !== path.basename(paths.runtimeTarball)
  ) {
    return false;
  }

  const [launcherDigest, runtimeDigest] = await Promise.all([
    sha256File(paths.launcherTarball),
    sha256File(paths.runtimeTarball),
  ]);

  return (
    result !== null &&
    result.platform === targetPlatform &&
    result.version === version &&
    result.source_fingerprint === sourceFingerprint &&
    installTarballs.launcher.sha256 === launcherDigest &&
    installTarballs.runtime.sha256 === runtimeDigest
  );
}

function runPackageBuild({
  paths,
  projectRoot,
  environment,
  spawnSyncImpl,
}) {
  const result = spawnSyncImpl(process.execPath, [paths.buildScript], {
    cwd: projectRoot,
    env: { ...environment, BOXTEAM_PROJECT_ROOT: projectRoot },
    stdio: "inherit",
  });
  if (result.error) {
    throw new Error(`本地发行包构建失败: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(
      `本地发行包构建失败: ${paths.buildScript} exit=${String(result.status)}`,
    );
  }
}

export function buildReleaseInstallCommand({
  prefix = null,
  platform = process.platform,
  architecture = process.arch,
  projectRoot = process.cwd(),
  version = BOXTEAM_VERSION,
} = {}) {
  const targetPlatform = resolveTargetPlatform(platform, architecture);
  const paths = resolveLocalPackagePaths({
    projectRoot: path.resolve(projectRoot),
    targetPlatform,
    version: requiredValue(version, "version"),
  });
  const args = [
    "install",
    "--global",
    "--no-audit",
    "--no-fund",
    "--offline",
    "--omit=optional",
  ];
  if (prefix !== null) {
    args.push("--prefix", path.resolve(requiredValue(prefix, "--prefix")));
  }
  args.push(paths.launcherTarball, paths.runtimeTarball);

  return Object.freeze({
    command: platform === "win32" ? "npm.cmd" : "npm",
    args: Object.freeze(args),
    targetPlatform,
    version: requiredValue(version, "version"),
    prefix: prefix === null ? null : path.resolve(prefix),
    packageTarballs: Object.freeze([
      paths.launcherTarball,
      paths.runtimeTarball,
    ]),
    buildScript: paths.buildScript,
    buildResult: paths.buildResult,
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
    throw new Error(`执行本地发行包安装失败: ${command}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(
      `本地发行包安装失败: ${command} ${args.join(" ")} exit=${String(result.status)}`,
    );
  }
}

function verifyInstalledRelease({
  command,
  cwd,
  environment,
  spawnSyncImpl = spawnSync,
}) {
  const rootArguments = ["root", "--global"];
  if (command.prefix !== null) {
    rootArguments.push("--prefix", command.prefix);
  }
  const rootResult = spawnSyncImpl(command.command, rootArguments, {
    cwd,
    env: environment,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  if (rootResult.error) {
    throw new Error(`读取全局 npm 安装目录失败: ${rootResult.error.message}`);
  }
  if (rootResult.status !== 0 || !rootResult.stdout?.trim()) {
    throw new Error(
      `读取全局 npm 安装目录失败: exit=${String(rootResult.status)}`,
    );
  }

  const globalRoot = path.resolve(rootResult.stdout.trim());
  const launcherRoot = path.join(globalRoot, LAUNCHER_PACKAGE_NAME);
  const runtimeRoot = path.join(
    globalRoot,
    "@boxteam",
    `runtime-${command.targetPlatform}`,
  );
  const launcherPackage = JSON.parse(
    readFileSync(path.join(launcherRoot, "package.json"), "utf8"),
  );
  const runtimePackage = JSON.parse(
    readFileSync(path.join(runtimeRoot, "package.json"), "utf8"),
  );
  if (
    launcherPackage.name !== LAUNCHER_PACKAGE_NAME ||
    launcherPackage.version !== command.version ||
    runtimePackage.name !== `@boxteam/runtime-${command.targetPlatform}` ||
    runtimePackage.version !== command.version
  ) {
    throw new Error(
      `全局 npm 安装包版本不匹配: expected=${command.version}, ` +
        `launcher=${String(launcherPackage.version)}, ` +
        `runtime=${String(runtimePackage.version)}`,
    );
  }

  const manifestPath = path.join(runtimeRoot, "runtime-manifest.json");
  const launcherPath = path.join(launcherRoot, "bin", "boxteam.mjs");
  if (!existsSync(manifestPath) || !existsSync(launcherPath)) {
    throw new Error(`全局 npm 安装缺少运行文件: ${globalRoot}`);
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const requiredRuntimePaths = [
    manifest.python_executable,
    manifest.application_root,
    manifest.web_assets,
    manifest.chromium_executable,
  ];
  for (const relativePath of requiredRuntimePaths) {
    if (
      typeof relativePath !== "string" ||
      !existsSync(path.join(runtimeRoot, relativePath))
    ) {
      throw new Error(
        `全局 npm runtime 缺少 manifest 文件: ${String(relativePath)}`,
      );
    }
  }
}

export async function ensureCurrentLocalPackage({
  command,
  projectRoot,
  environment = process.env,
  spawnSyncImpl = spawnSync,
} = {}) {
  const sourceFingerprint = computeBuildInputFingerprint({
    projectRoot,
    targetPlatform: command.targetPlatform,
  });
  const buildMetadata = {
    paths: {
      buildResult: command.buildResult,
      launcherTarball: command.packageTarballs[0],
      runtimeTarball: command.packageTarballs[1],
    },
    targetPlatform: command.targetPlatform,
    version: command.version,
    sourceFingerprint,
  };
  if (await buildResultMatches(buildMetadata)) {
    process.stdout.write(
      `本地发行包与当前源码匹配: ${command.targetPlatform} ${command.version}\n`,
    );
    return;
  }

  process.stdout.write(
    `本地发行包缺失或已过期，重新打包: ${command.targetPlatform} ${BOXTEAM_VERSION}\n`,
  );
  runPackageBuild({
    paths: {
      buildScript: command.buildScript,
    },
    projectRoot,
    environment,
    spawnSyncImpl,
  });

  const rebuiltFingerprint = computeBuildInputFingerprint({
    projectRoot,
    targetPlatform: command.targetPlatform,
  });
  if (
    rebuiltFingerprint !== sourceFingerprint ||
    !(await buildResultMatches(buildMetadata))
  ) {
    throw new Error(
      `本地发行包构建结果与当前源码不一致: ${command.targetPlatform}`,
    );
  }
}

export async function main(args = process.argv.slice(2)) {
  const options = parseInstallArguments(args);
  if (options.help) {
    process.stdout.write(
      "用法: bun run scripts/install/install-release.mjs [--prefix <目录>]\n" +
        "安装本地源码打包的 BoxTeam CLI 和当前平台 runtime；缺失或过期时自动重新打包。\n" +
        "安装完成后使用 boxteam start 启动 packaged Gateway。\n",
    );
    return;
  }

  const projectRoot = path.resolve(
    process.env.BOXTEAM_PROJECT_ROOT?.trim() || process.cwd(),
  );
  const command = buildReleaseInstallCommand({
    prefix: options.prefix,
    projectRoot,
  });
  await ensureCurrentLocalPackage({ command, projectRoot });
  runReleaseInstall({
    command: command.command,
    args: command.args,
    cwd: projectRoot,
  });
  verifyInstalledRelease({
    command,
    cwd: projectRoot,
    environment: process.env,
  });
  process.stdout.write(
    `本地发行包安装完成: ${command.version}\n` +
      "下一步: boxteam start（Gateway 将在 8014 托管打包 Web UI）\n",
  );
}

const currentModulePath = fileURLToPath(import.meta.url);
if (process.argv[1] && path.resolve(process.argv[1]) === currentModulePath) {
  await main();
}
