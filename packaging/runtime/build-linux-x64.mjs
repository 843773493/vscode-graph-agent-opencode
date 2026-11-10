import {
  cpSync,
  existsSync,
  mkdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import {
  BOXTEAM_VERSION,
  NODE_RUNTIME_DEPENDENCIES,
  NODE_RUNTIME_LINUX_X64,
  PYTHON_RUNTIME,
} from "./versions.mjs";
import {
  sha256File,
  runtimeAssetUrl,
  stageRuntimeDownloaderPackage,
  stageRuntimeAndLauncherPackages,
  writeRuntimeSizeReport,
} from "./runtime-release-assets.mjs";
import { downloadPinnedLinuxPython, downloadPinnedLinuxNode } from "./pinned-archive-download.mjs";
import { copyApplicationSources } from "./application-sources.mjs";
import { packDeveloperTools } from "../../scripts/plugins/pack-developer-tools.mjs";
import { packMcpStdio } from "../../scripts/plugins/pack-mcp-stdio.mjs";
import {
  beginRuntimeBuild,
  publishRuntimeBuildResult,
} from "./runtime-build-result.mjs";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const outputRoot = path.join(projectRoot, "out", "packaging", "linux-x64");
const downloadRoot = path.join(outputRoot, "downloads");
const stageRoot = path.join(outputRoot, "stage");
const runtimePackageRoot = path.join(stageRoot, "runtime-linux-x64", "package");
const launcherPackageRoot = path.join(stageRoot, "launcher", "package");
const tarballRoot = path.join(outputRoot, "tarballs");
const releaseAssetRoot = path.join(outputRoot, "release-assets");

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd ?? projectRoot,
    env: options.env ?? process.env,
    encoding: "utf8",
    stdio: options.capture ? "pipe" : "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `命令失败 (${String(result.status)}): ${command} ${args.join(" ")}\n` +
        `${result.stderr ?? ""}`,
    );
  }
  return result.stdout?.trim() ?? "";
}

function installPythonDependencies(pythonExecutable) {
  const requirementsPath = path.join(outputRoot, "requirements.locked.txt");
  run("uv", [
    "export",
    "--locked",
    "--no-dev",
    "--format",
    "requirements-txt",
    "--output-file",
    requirementsPath,
  ]);
  run("uv", [
    "pip",
    "install",
    "--python",
    pythonExecutable,
    "--requirements",
    requirementsPath,
  ]);
}

function installNodeDependencies(applicationRoot, nodeExecutable) {
  cpSync(
    path.join(projectRoot, "packaging", "runtime", "node-package.json"),
    path.join(applicationRoot, "package.json"),
  );
  run("bun", ["install", "--production", "--exact", "--ignore-scripts"], {
    cwd: applicationRoot,
    env: { ...process.env, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1" },
  });
  const nodePtyRoot = path.join(applicationRoot, "node_modules", "node-pty");
  run(nodeExecutable, [path.join(projectRoot, "node_modules/node-gyp/bin/node-gyp.js"), "rebuild"], {
    cwd: nodePtyRoot,
  });
  run(nodeExecutable, ["--input-type=module", "--eval", "await import('./src/shared/serverPluginRuntime.mjs');"], {
    cwd: applicationRoot,
  });
}

function installChromium(applicationRoot, chromiumRoot, nodeExecutable) {
  const chromiumCacheRoot = path.join(downloadRoot, "playwright-browsers");
  mkdirSync(chromiumCacheRoot, { recursive: true });
  const environment = {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: chromiumCacheRoot,
  };
  run(
    nodeExecutable,
    [
      path.join(applicationRoot, "node_modules", "playwright", "cli.js"),
      "install",
      "chromium",
    ],
    { cwd: applicationRoot, env: environment },
  );
  const executable = run(
    nodeExecutable,
    [
      "--input-type=module",
      "--eval",
      "import { chromium } from 'playwright'; process.stdout.write(chromium.executablePath());",
    ],
    { cwd: applicationRoot, env: environment, capture: true },
  );
  if (!existsSync(executable)) {
    throw new Error(`Playwright 声明的 Chromium 不存在: ${executable}`);
  }
  const executableRelativePath = path.relative(chromiumCacheRoot, executable);
  const [browserDirectory] = executableRelativePath.split(path.sep);
  if (
    !browserDirectory ||
    browserDirectory === "." ||
    browserDirectory === ".." ||
    executableRelativePath.startsWith(`..${path.sep}`)
  ) {
    throw new Error(`Playwright Chromium 不在浏览器缓存目录内: ${executable}`);
  }
  cpSync(
    path.join(chromiumCacheRoot, browserDirectory),
    path.join(chromiumRoot, browserDirectory),
    { recursive: true },
  );
  const packagedExecutable = path.join(chromiumRoot, executableRelativePath);
  if (!existsSync(packagedExecutable)) {
    throw new Error(`复制后缺少 Chromium: ${packagedExecutable}`);
  }
  return packagedExecutable;
}

function writeRuntimeMetadata({ chromiumExecutable }) {
  const manifest = {
    schema_version: 1,
    distribution: "npm",
    version: BOXTEAM_VERSION,
    // npm pack 会排除包内符号链接，因此必须指向实际解释器文件。
    python_executable: "python/bin/python3.12",
    application_root: "application",
    config_resources: {
      gateway_inline: "application/configs/gateway_inline.jsonc",
      gateway_schema: "application/configs/gateway_schema.jsonc",
      workspace_inline: "application/configs/workspace_inline.jsonc",
      workspace_schema: "application/configs/workspace_schema.jsonc",
    },
    skill_resources: "application/resources/skills",
    web_assets: "web",
    chromium_executable: path.relative(runtimePackageRoot, chromiumExecutable),
    node: {
      source: "bundled",
      executable: "node/bin/node",
    },
  };
  writeFileSync(
    path.join(runtimePackageRoot, "runtime-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  writeFileSync(
    path.join(runtimePackageRoot, "THIRD_PARTY_LICENSES.json"),
    `${JSON.stringify(
      {
        python: PYTHON_RUNTIME,
        node_runtime: NODE_RUNTIME_LINUX_X64,
        node_dependencies: NODE_RUNTIME_DEPENDENCIES,
        playwright: "Apache-2.0",
        chromium: "Chromium 项目随附许可文件，仅打包 manifest 指向的浏览器资源",
      },
      null,
      2,
    )}\n`,
  );
}

function npmPack(packageRoot, destinationRoot) {
  const filename = run(
    "npm",
    ["pack", "--silent", "--pack-destination", destinationRoot],
    { cwd: packageRoot, capture: true },
  );
  const outputLines = filename.split(/\r?\n/).filter(Boolean);
  if (outputLines.length !== 1 || !outputLines[0].endsWith(".tgz")) {
    throw new Error(`npm pack 返回未知结果: ${filename}`);
  }
  return path.join(destinationRoot, outputLines[0]);
}

async function main() {
  if (process.platform !== "linux" || process.arch !== "x64") {
    throw new Error(
      `linux-x64 构建器不支持当前平台: ${process.platform}-${process.arch}`,
    );
  }
  const startingSourceFingerprint = beginRuntimeBuild({
    projectRoot,
    targetPlatform: "linux-x64",
    buildResultPath: path.join(outputRoot, "build-result.json"),
  });
  rmSync(stageRoot, { recursive: true, force: true });
  rmSync(tarballRoot, { recursive: true, force: true });
  rmSync(releaseAssetRoot, { recursive: true, force: true });
  mkdirSync(runtimePackageRoot, { recursive: true });
  mkdirSync(tarballRoot, { recursive: true });
  mkdirSync(releaseAssetRoot, { recursive: true });

  const pythonArchive = await downloadPinnedLinuxPython(downloadRoot);
  run("tar", ["-xzf", pythonArchive, "-C", runtimePackageRoot]);
  const pythonExecutable = path.join(
    runtimePackageRoot,
    "python",
    "bin",
    "python3",
  );
  if (!existsSync(pythonExecutable)) {
    throw new Error(`解压后缺少 Python: ${pythonExecutable}`);
  }

  const nodeArchive = await downloadPinnedLinuxNode(downloadRoot);
  const bundledNodeRoot = path.join(runtimePackageRoot, "node");
  mkdirSync(bundledNodeRoot, { recursive: true });
  run("tar", ["-xzf", nodeArchive, "-C", bundledNodeRoot, "--strip-components=1",
    `node-v${NODE_RUNTIME_LINUX_X64.version}-linux-x64/bin/node`,
    `node-v${NODE_RUNTIME_LINUX_X64.version}-linux-x64/LICENSE`,
  ]);
  const nodeExecutable = path.join(bundledNodeRoot, "bin", "node");
  if (!existsSync(nodeExecutable)) throw new Error(`Node 压缩包缺少 bin/node: ${nodeArchive}`);

  const applicationRoot = path.join(runtimePackageRoot, "application");
  mkdirSync(applicationRoot, { recursive: true });
  copyApplicationSources(projectRoot, applicationRoot);
  // 内建 SQLite 不导出 C 符号；探针只使用连接注入的 SQLite extension API。
  run("cc", [
    "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
    "-o", path.join(applicationRoot, "app/core/sqlite_vfs_probe.so"),
    path.join(applicationRoot, "app/core/sqlite_vfs_probe.c"),
  ]);
  installPythonDependencies(pythonExecutable);
  run(pythonExecutable, [
    "-c",
    "from app.core.sqlite_state import sqlite_vfs_max_pathname; " +
      "print('SQLite VFS mxPathname:', sqlite_vfs_max_pathname())",
  ], { cwd: applicationRoot });
  const bunExecutable = Bun.which("bun");
  const uvExecutable = Bun.which("uv");
  if (!bunExecutable || !uvExecutable) {
    throw new Error("发布构建需要明确的 Bun 和 uv 构建工具");
  }
  const nodeRuntime = JSON.parse(run(nodeExecutable, ["--print",
    "JSON.stringify({version:process.versions.node,abi:process.versions.modules,glibc:process.report.getReport().header.glibcVersionRuntime})",
  ], { capture: true }));
  if (!nodeRuntime.glibc) throw new Error("linux-x64 发布构建需要 GNU libc 目标");
  installNodeDependencies(applicationRoot, nodeExecutable);
  const pluginBuild = {
    projectRoot, nodeExecutable, bunExecutable, uvExecutable, pythonExecutable,
    nodeGypEntrypoint: path.join(projectRoot, "node_modules/node-gyp/bin/node-gyp.js"),
    target: {
      os: "linux", architecture: "x64", system_baseline: `glibc-${nodeRuntime.glibc}`,
      node: { version: nodeRuntime.version, abi: nodeRuntime.abi },
      python: { version: PYTHON_RUNTIME.version, abi: "cp312" },
    },
  };
  await packDeveloperTools({ ...pluginBuild, output: path.join(applicationRoot, "resources/plugins/developer-tools.zip") });
  await packMcpStdio({ ...pluginBuild, output: path.join(applicationRoot, "resources/plugins/mcp-stdio.zip") });

  run("bun", ["run", "build"], {
    cwd: path.join(projectRoot, "src", "clients", "workbench"),
  });
  cpSync(
    path.join(projectRoot, "src", "clients", "workbench", "dist"),
    path.join(runtimePackageRoot, "web"),
    { recursive: true },
  );
  const chromiumExecutable = installChromium(
    applicationRoot,
    path.join(runtimePackageRoot, "chromium"),
    nodeExecutable,
  );
  writeRuntimeMetadata({ chromiumExecutable });
  stageRuntimeAndLauncherPackages({
    projectRoot, platform: "linux-x64", runtimePackageRoot, launcherPackageRoot,
  });

  const fullRuntimeTarball = npmPack(runtimePackageRoot, releaseAssetRoot);
  const downloader = await stageRuntimeDownloaderPackage({
    projectRoot,
    sourcePackagePath: path.join(
      projectRoot,
      "packages",
      "runtime-linux-x64",
      "package.json",
    ),
    stageRoot: path.join(stageRoot, "runtime-downloader-linux-x64"),
    platform: "linux-x64",
    releaseAssetPath: fullRuntimeTarball,
  });
  const npmTarballs = [
    npmPack(downloader.packageRoot, tarballRoot),
    npmPack(launcherPackageRoot, tarballRoot),
  ];
  const releaseAssets = [
    {
      filename: path.basename(fullRuntimeTarball),
      path: fullRuntimeTarball,
      url: runtimeAssetUrl("linux-x64"),
      sha256: downloader.releaseAsset.sha256,
    },
  ];
  writeRuntimeSizeReport({
    runtimePackageRoot, outputRoot, npmTarballs,
    assetGroups: { release_assets: releaseAssets },
  });
  const launcherTarball = npmTarballs.find(
    (tarball) => path.basename(tarball) === `boxteam-${BOXTEAM_VERSION}.tgz`,
  );
  const runtimeAsset = releaseAssets.find(
    (asset) =>
      asset.filename === `boxteam-runtime-linux-x64-${BOXTEAM_VERSION}.tgz`,
  );
  if (!launcherTarball || !runtimeAsset) {
    throw new Error("本地安装 tarball 清单缺少 Launcher 或 Linux runtime");
  }
  publishRuntimeBuildResult({
    projectRoot,
    targetPlatform: "linux-x64",
    buildResultPath: path.join(outputRoot, "build-result.json"),
    startingSourceFingerprint,
    metadata: {
      platform: "linux-x64",
      version: BOXTEAM_VERSION,
      install_tarballs: {
        launcher: {
          filename: path.basename(launcherTarball),
          sha256: await sha256File(launcherTarball),
        },
        runtime: {
          filename: runtimeAsset.filename,
          sha256: runtimeAsset.sha256,
        },
      },
      npm_tarballs: npmTarballs,
      release_assets: releaseAssets,
    },
  });
  process.stdout.write(`构建完成: ${outputRoot}\n`);
}

await main();
