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
  PYTHON_RUNTIME,
} from "./versions.mjs";
import {
  sha256File,
  runtimeAssetUrl,
  stageRuntimeDownloaderPackage,
  stageRuntimeAndLauncherPackages,
  writeRuntimeSizeReport,
} from "./runtime-release-assets.mjs";
import { downloadPinnedLinuxPython } from "./pinned-archive-download.mjs";
import { copyApplicationSources } from "./application-sources.mjs";
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
const NODE_GYP_VERSION = "10.3.1";

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

function installNodeDependencies(applicationRoot) {
  cpSync(
    path.join(projectRoot, "packaging", "runtime", "node-package.json"),
    path.join(applicationRoot, "package.json"),
  );
  // Bun 不会像 npm 一样为 node-pty 安装脚本提供 node-gyp；先跳过生命周期脚本，
  // 再用 Bun 的 x 命令调用 node-gyp 编译 Linux 原生模块，避免把构建工具打进运行时。
  run("bun", ["install", "--production", "--exact", "--ignore-scripts"], {
    cwd: applicationRoot,
    env: {
      ...process.env,
      PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1",
    },
  });
  const nodePtyRoot = path.join(applicationRoot, "node_modules", "node-pty");
  run("bun", ["x", `node-gyp@${NODE_GYP_VERSION}`, "rebuild"], {
    cwd: nodePtyRoot,
  });
  run("bun", ["scripts/post-install.js"], { cwd: nodePtyRoot });
}

function installChromium(applicationRoot, chromiumRoot) {
  const chromiumCacheRoot = path.join(downloadRoot, "playwright-browsers");
  mkdirSync(chromiumCacheRoot, { recursive: true });
  const environment = {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: chromiumCacheRoot,
  };
  run(
    process.execPath,
    [
      path.join(applicationRoot, "node_modules", "playwright", "cli.js"),
      "install",
      "chromium",
    ],
    { cwd: applicationRoot, env: environment },
  );
  const executable = run(
    process.execPath,
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
      source: "launcher",
      executable: null,
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

  const applicationRoot = path.join(runtimePackageRoot, "application");
  mkdirSync(applicationRoot, { recursive: true });
  copyApplicationSources(projectRoot, applicationRoot);
  installPythonDependencies(pythonExecutable);
  installNodeDependencies(applicationRoot);

  run("bun", ["run", "build"], {
    cwd: path.join(projectRoot, "src", "clients", "web"),
  });
  cpSync(
    path.join(projectRoot, "src", "clients", "web", "dist"),
    path.join(runtimePackageRoot, "web"),
    { recursive: true },
  );
  const chromiumExecutable = installChromium(
    applicationRoot,
    path.join(runtimePackageRoot, "chromium"),
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
