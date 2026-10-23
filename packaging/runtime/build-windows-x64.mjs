import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import {
  BOXTEAM_VERSION,
  NODE_RUNTIME_WINDOWS_X64,
  NODE_RUNTIME_DEPENDENCIES,
  PYTHON_RUNTIME_WINDOWS_X64,
} from "./versions.mjs";
import {
  sha256File,
  runtimeAssetUrl,
  stageRuntimeDownloaderPackage,
  stageRuntimeAndLauncherPackages,
  writeRuntimeSizeReport,
} from "./runtime-release-assets.mjs";
import { installWindowsPythonDependencies } from "./windows-python-dependencies.mjs";
import {
  downloadPinnedWindowsPython,
  downloadPinnedWindowsNode,
} from "./pinned-archive-download.mjs";
import { copyApplicationSources } from "./application-sources.mjs";
import {
  beginRuntimeBuild,
  publishRuntimeBuildResult,
} from "./runtime-build-result.mjs";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const outputRoot = path.join(projectRoot, "out", "packaging", "windows-x64");
const downloadRoot = path.join(outputRoot, "downloads");
const pythonDownloadRoot = path.resolve(
  process.env.BOXTEAM_PYTHON_DOWNLOAD_ROOT ?? downloadRoot,
);
const nodeDownloadRoot = path.resolve(
  process.env.BOXTEAM_NODE_DOWNLOAD_ROOT ?? downloadRoot,
);
const stageRoot = path.join(outputRoot, "stage");
const runtimePackageRoot = path.join(
  stageRoot,
  "runtime-windows-x64",
  "package",
);
const launcherPackageRoot = path.join(stageRoot, "launcher", "package");
const tarballRoot = path.join(outputRoot, "tarballs");
const releaseAssetRoot = path.join(outputRoot, "release-assets");
const standaloneRoot = path.join(outputRoot, "standalone");
const installerRoot = path.join(outputRoot, "installer");
const standaloneStageRoot = path.join(stageRoot, "standalone");
const standaloneDirectoryName = `boxteam-windows-x64-${BOXTEAM_VERSION}`;
const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
const crossBuild = process.argv.includes("--cross");
function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd ?? projectRoot,
    env: options.env ?? process.env,
    encoding: "utf8",
    shell: options.shell ?? false,
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

function precompilePythonBytecode(pythonExecutable, applicationRoot) {
  // 编译第三方依赖（site-packages）与应用源码的字节码缓存，随安装包分发，
  // 避免用户首次运行时现场编译导致冷启动超时（Windows 上可能超过 120 秒）。
  const compileArguments = [
    "-m",
    "compileall",
    "-q",
    "-j",
    "0",
    "-f",
  ];
  const sourceRoots = [
    path.join(runtimePackageRoot, "python", "Lib", "site-packages"),
    path.join(applicationRoot, "app"),
  ];
  if (crossBuild) {
    // 目标 Python 无法在 Linux 上执行；主次版本相同时，主机 compileall 生成的字节码可由 Windows 运行时使用。
    compileArguments.push("-s", runtimePackageRoot);
    run("uv", ["run", "python", ...compileArguments, ...sourceRoots]);
    return;
  }
  run(pythonExecutable, [...compileArguments, ...sourceRoots]);
}

function installNodeDependencies(applicationRoot) {
  cpSync(
    path.join(projectRoot, "packaging", "runtime", "node-package.json"),
    path.join(applicationRoot, "package.json"),
  );
  const installArguments = ["install", "--production", "--exact"];
  if (crossBuild) {
    // Bun 只负责解析和下载目标平台依赖；node-pty 的 Windows prebuild 已随 npm 包提供，不执行 Linux 安装脚本。
    installArguments.push("--os", "win32", "--cpu", "x64", "--ignore-scripts");
  }
  run("bun", installArguments, {
    cwd: applicationRoot,
    env: {
      ...process.env,
      PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1",
    },
  });
}

async function installChromium(applicationRoot, chromiumRoot) {
  const configuredCacheRoot =
    process.env.BOXTEAM_PLAYWRIGHT_BROWSERS_PATH?.trim() || null;
  const ownsChromiumCache = configuredCacheRoot === null;
  const chromiumCacheRoot = ownsChromiumCache
    ? path.join(downloadRoot, "playwright-browsers")
    : path.resolve(configuredCacheRoot);
  mkdirSync(chromiumCacheRoot, { recursive: true });
  const baseEnvironment = {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: chromiumCacheRoot,
    PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT: "120000",
    ...(crossBuild ? { PLAYWRIGHT_HOST_PLATFORM_OVERRIDE: "win64" } : {}),
  };
  let environment = baseEnvironment;
  let lastError = null;
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    if (ownsChromiumCache) {
      rmSync(chromiumCacheRoot, { recursive: true, force: true });
    }
    mkdirSync(chromiumCacheRoot, { recursive: true });
    const downloadHost =
      attempt % 2 === 0
        ? "https://playwright.download.prss.microsoft.com"
        : null;
    environment = {
      ...baseEnvironment,
      ...(downloadHost === null
        ? {}
        : { PLAYWRIGHT_DOWNLOAD_HOST: downloadHost }),
    };
    try {
      run(
        process.execPath,
        [
          path.join(applicationRoot, "node_modules", "playwright", "cli.js"),
          "install",
          "chromium",
        ],
        { cwd: applicationRoot, env: environment },
      );
      lastError = null;
      break;
    } catch (error) {
      lastError = error;
      if (attempt < 5) {
        await new Promise((resolve) => setTimeout(resolve, 1_000));
      }
    }
  }
  if (lastError !== null) throw lastError;
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

function writeRuntimeMetadata({
  runtimeRoot = runtimePackageRoot,
  chromiumExecutable,
  chromiumExecutableRelative,
  distribution = "npm",
  nodeSource = "launcher",
  nodeExecutable = null,
  nodeRuntime = null,
}) {
  const manifest = {
    schema_version: 1,
    distribution,
    version: BOXTEAM_VERSION,
    // npm pack 会排除包内符号链接，因此必须指向实际解释器文件。
    python_executable: "python/python.exe",
    application_root: "application",
    config_resources: {
      gateway_inline: "application/configs/gateway_inline.jsonc",
      gateway_schema: "application/configs/gateway_schema.jsonc",
      workspace_inline: "application/configs/workspace_inline.jsonc",
      workspace_schema: "application/configs/workspace_schema.jsonc",
    },
    skill_resources: "application/resources/skills",
    web_assets: "web",
    chromium_executable:
      chromiumExecutableRelative ?? path.relative(runtimeRoot, chromiumExecutable),
    node: {
      source: nodeSource,
      executable: nodeExecutable,
    },
  };
  writeFileSync(
    path.join(runtimeRoot, "runtime-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  writeFileSync(
    path.join(runtimeRoot, "THIRD_PARTY_LICENSES.json"),
    `${JSON.stringify(
      {
        python: PYTHON_RUNTIME_WINDOWS_X64,
        node_runtime: nodeRuntime,
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
    npmCommand,
    ["pack", "--silent", "--pack-destination", destinationRoot],
    { cwd: packageRoot, capture: true, shell: true },
  );
  const outputLines = filename.split(/\r?\n/).filter(Boolean);
  if (outputLines.length !== 1 || !outputLines[0].endsWith(".tgz")) {
    throw new Error(`npm pack 返回未知结果: ${filename}`);
  }
  return path.join(destinationRoot, outputLines[0]);
}

function writeStandaloneLauncherScripts(packageRoot) {
  writeFileSync(
    path.join(packageRoot, "boxteam.cmd"),
    [
      "@echo off",
      "setlocal",
      'set "BOXTEAM_RUNTIME_MANIFEST=%~dp0runtime\\runtime-manifest.json"',
      '"%~dp0runtime\\node\\node.exe" "%~dp0launcher\\bin\\boxteam.mjs" %*',
      "exit /b %ERRORLEVEL%",
      "",
    ].join("\r\n"),
  );
  writeFileSync(
    path.join(packageRoot, "boxteam-doctor.cmd"),
    [
      "@echo off",
      "setlocal",
      'set "BOXTEAM_RUNTIME_MANIFEST=%~dp0runtime\\runtime-manifest.json"',
      '"%~dp0runtime\\node\\node.exe" "%~dp0launcher\\bin\\boxteam.mjs" doctor %*',
      "exit /b %ERRORLEVEL%",
      "",
    ].join("\r\n"),
  );
  writeFileSync(
    path.join(packageRoot, "README.txt"),
    [
      "BoxTeam Windows x64 便携版",
      "",
      "运行：双击 BoxTeam.exe；也可以在 PowerShell 中执行 .\\boxteam.cmd。",
      "诊断：双击 BoxTeamDoctor.exe，或执行 .\\boxteam-doctor.cmd --json。",
      "",
      "本目录已经包含 Node、Python、Web UI 和 Chromium，无需安装 npm、Node、Python 或 Bun。",
      "安装版默认安装到 C:\\Program Files\\BoxTeam，可在安装器中选择其他目录。",
      "请保持目录结构完整；配置和运行日志默认写入当前用户的 .boxteams 目录。",
      "",
    ].join("\r\n"),
  );
}

function stageStandalonePackage(nodeArchive) {
  const packageRoot = path.join(standaloneStageRoot, standaloneDirectoryName);
  const runtimeRoot = path.join(packageRoot, "runtime");
  const launcherRoot = path.join(packageRoot, "launcher");
  const nodeExtractRoot = path.join(standaloneStageRoot, "node-extract");
  const standaloneArchive = path.join(
    standaloneRoot,
    `${standaloneDirectoryName}.zip`,
  );
  rmSync(packageRoot, { recursive: true, force: true });
  rmSync(nodeExtractRoot, { recursive: true, force: true });
  rmSync(standaloneArchive, { force: true });
  mkdirSync(standaloneRoot, { recursive: true });
  mkdirSync(nodeExtractRoot, { recursive: true });
  cpSync(runtimePackageRoot, runtimeRoot, { recursive: true });
  if (crossBuild) {
    run("unzip", ["-q", nodeArchive, "-d", nodeExtractRoot]);
  } else {
    run("tar", ["-xf", nodeArchive, "-C", nodeExtractRoot]);
  }
  const [nodeDirectory] = readdirSync(nodeExtractRoot, {
    withFileTypes: true,
  }).filter((entry) => entry.isDirectory());
  if (!nodeDirectory) {
    throw new Error(`Windows Node 压缩包缺少顶层目录: ${nodeArchive}`);
  }
  const bundledNodeSource = path.join(
    nodeExtractRoot,
    nodeDirectory.name,
    "node.exe",
  );
  if (!existsSync(bundledNodeSource)) {
    throw new Error(`Windows Node 压缩包缺少 node.exe: ${nodeArchive}`);
  }
  const bundledNodeRoot = path.join(runtimeRoot, "node");
  mkdirSync(bundledNodeRoot, { recursive: true });
  cpSync(bundledNodeSource, path.join(bundledNodeRoot, "node.exe"));

  const npmManifest = JSON.parse(
    readFileSync(path.join(runtimePackageRoot, "runtime-manifest.json"), "utf8"),
  );
  writeRuntimeMetadata({
    runtimeRoot,
    chromiumExecutableRelative: npmManifest.chromium_executable,
    distribution: "standalone",
    nodeSource: "bundled",
    nodeExecutable: "node/node.exe",
    nodeRuntime: NODE_RUNTIME_WINDOWS_X64,
  });
  cpSync(launcherPackageRoot, launcherRoot, { recursive: true });
  const windowsLauncher = buildWindowsLauncher();
  cpSync(windowsLauncher, path.join(packageRoot, "BoxTeam.exe"));
  cpSync(windowsLauncher, path.join(packageRoot, "BoxTeamDoctor.exe"));
  writeStandaloneLauncherScripts(packageRoot);

  if (crossBuild) {
    run("zip", ["-q", "-r", standaloneArchive, standaloneDirectoryName], {
      cwd: standaloneStageRoot,
    });
  } else {
    // TODO: Windows 旧版 tar.exe 可能不支持按扩展名生成 ZIP；当前支持矩阵为 Windows 10/11 与 Server 2022。
    run("tar", [
      "-a",
      "-c",
      "-f",
      standaloneArchive,
      "-C",
      standaloneStageRoot,
      standaloneDirectoryName,
    ]);
  }
  if (!existsSync(standaloneArchive)) {
    throw new Error(`便携版 ZIP 未生成: ${standaloneArchive}`);
  }
  return standaloneArchive;
}

function buildWindowsLauncher() {
  const compiler =
    process.env.BOXTEAM_WINDOWS_CC ??
    (crossBuild ? "x86_64-w64-mingw32-gcc" : "gcc");
  const launcherOutputRoot = path.join(stageRoot, "windows-launcher");
  const launcherOutput = path.join(launcherOutputRoot, "BoxTeam.exe");
  mkdirSync(launcherOutputRoot, { recursive: true });
  run(compiler, [
    "-municode",
    "-mconsole",
    "-O2",
    "-s",
    path.join(projectRoot, "packaging", "runtime", "windows-launcher.c"),
    "-o",
    launcherOutput,
  ]);
  if (!existsSync(launcherOutput)) {
    throw new Error(`Windows 原生启动器未生成: ${launcherOutput}`);
  }
  return launcherOutput;
}

function buildWindowsInstaller(packageRoot) {
  const installerArchive = path.join(
    installerRoot,
    `${standaloneDirectoryName}-setup.exe`,
  );
  const nsisCommand = process.env.BOXTEAM_NSIS_BIN ?? "makensis";
  const nsisEnvironment = process.env.BOXTEAM_NSIS_ROOT
    ? { ...process.env, NSISDIR: process.env.BOXTEAM_NSIS_ROOT }
    : process.env;
  mkdirSync(installerRoot, { recursive: true });
  run(nsisCommand, [
    `-DVERSION=${BOXTEAM_VERSION}`,
    `-DINPUTDIR=${packageRoot.split(path.sep).join("/")}`,
    `-DOUTPUT=${installerArchive.split(path.sep).join("/")}`,
    path.join(projectRoot, "packaging", "runtime", "boxteam-installer.nsi"),
  ], {
    env: nsisEnvironment,
  });
  if (!existsSync(installerArchive)) {
    throw new Error(`Windows 安装器未生成: ${installerArchive}`);
  }
  return installerArchive;
}

async function main() {
  const nativeWindowsBuild =
    process.platform === "win32" && process.arch === "x64" && !crossBuild;
  const linuxCrossBuild =
    process.platform === "linux" && process.arch === "x64" && crossBuild;
  if (!nativeWindowsBuild && !linuxCrossBuild) {
    throw new Error(
      `windows-x64 构建器不支持当前模式: host=${process.platform}-${process.arch} cross=${String(crossBuild)}`,
    );
  }
  const startingSourceFingerprint = beginRuntimeBuild({
    projectRoot,
    targetPlatform: "windows-x64",
    buildResultPath: path.join(outputRoot, "build-result.json"),
  });
  rmSync(stageRoot, { recursive: true, force: true });
  rmSync(tarballRoot, { recursive: true, force: true });
  rmSync(releaseAssetRoot, { recursive: true, force: true });
  rmSync(standaloneRoot, { recursive: true, force: true });
  rmSync(installerRoot, { recursive: true, force: true });
  mkdirSync(runtimePackageRoot, { recursive: true });
  mkdirSync(tarballRoot, { recursive: true });
  mkdirSync(releaseAssetRoot, { recursive: true });
  mkdirSync(standaloneRoot, { recursive: true });
  mkdirSync(installerRoot, { recursive: true });

  const pythonArchive = await downloadPinnedWindowsPython(pythonDownloadRoot);
  const nodeArchive = await downloadPinnedWindowsNode(nodeDownloadRoot);
  // TODO: Windows 目标依赖系统提供的 tar.exe；若未来支持没有 tar.exe 的旧版 Windows，需改为内置解压实现。
  run("tar", ["-xzf", pythonArchive, "-C", runtimePackageRoot]);
  const pythonExecutable = path.join(
    runtimePackageRoot,
    "python",
    "python.exe",
  );
  if (!existsSync(pythonExecutable)) {
    throw new Error(`解压后缺少 Windows Python: ${pythonExecutable}`);
  }

  const applicationRoot = path.join(runtimePackageRoot, "application");
  mkdirSync(applicationRoot, { recursive: true });
  copyApplicationSources(projectRoot, applicationRoot);
  installWindowsPythonDependencies({
    pythonExecutable, outputRoot, runtimePackageRoot, crossBuild, run,
  });
  // 预编译 Python 字节码缓存（__pycache__/.pyc）：
  // - 打包产物首次运行时，Python 需要现场编译 site-packages 与 app 源码的全部字节码，
  //   在普通 Windows 机器上冷启动可能超过 120 秒，导致 Gateway 等待后端就绪超时退出。
  // - 这里提前用 compileall 生成字节码缓存并打进安装包，显著缩短用户首次启动时间。
  precompilePythonBytecode(pythonExecutable, applicationRoot);
  installNodeDependencies(applicationRoot);

  run("bun", ["run", "build"], {
    cwd: path.join(projectRoot, "src", "clients", "web"),
  });
  cpSync(
    path.join(projectRoot, "src", "clients", "web", "dist"),
    path.join(runtimePackageRoot, "web"),
    { recursive: true },
  );
  const chromiumExecutable = await installChromium(
    applicationRoot,
    path.join(runtimePackageRoot, "chromium"),
  );
  writeRuntimeMetadata({ chromiumExecutable });
  stageRuntimeAndLauncherPackages({
    projectRoot, platform: "windows-x64", runtimePackageRoot, launcherPackageRoot,
  });

  const fullRuntimeTarball = npmPack(runtimePackageRoot, releaseAssetRoot);
  const downloader = await stageRuntimeDownloaderPackage({
    projectRoot,
    sourcePackagePath: path.join(
      projectRoot,
      "packages",
      "runtime-windows-x64",
      "package.json",
    ),
    stageRoot: path.join(stageRoot, "runtime-downloader-windows-x64"),
    platform: "windows-x64",
    releaseAssetPath: fullRuntimeTarball,
  });
  const npmTarballs = [
    npmPack(downloader.packageRoot, tarballRoot),
    npmPack(launcherPackageRoot, tarballRoot),
  ];
  const standaloneArchive = stageStandalonePackage(nodeArchive);
  const installerArchive = buildWindowsInstaller(
    path.join(standaloneStageRoot, standaloneDirectoryName),
  );
  const releaseAssets = [
    {
      filename: path.basename(fullRuntimeTarball),
      path: fullRuntimeTarball,
      url: runtimeAssetUrl("windows-x64"),
      sha256: downloader.releaseAsset.sha256,
    },
  ];
  const standaloneAssets = [
    {
      filename: path.basename(standaloneArchive),
      path: standaloneArchive,
    },
  ];
  const installerAssets = [
    {
      filename: path.basename(installerArchive),
      path: installerArchive,
    },
  ];
  writeRuntimeSizeReport({
    runtimePackageRoot, outputRoot, npmTarballs,
    assetGroups: {
      release_assets: releaseAssets,
      standalone_assets: standaloneAssets,
      installer_assets: installerAssets,
    },
  });
  const launcherTarball = npmTarballs.find(
    (tarball) => path.basename(tarball) === `boxteam-${BOXTEAM_VERSION}.tgz`,
  );
  const runtimeAsset = releaseAssets.find(
    (asset) =>
      asset.filename === `boxteam-runtime-windows-x64-${BOXTEAM_VERSION}.tgz`,
  );
  if (!launcherTarball || !runtimeAsset) {
    throw new Error("本地安装 tarball 清单缺少 Launcher 或 Windows runtime");
  }
  publishRuntimeBuildResult({
    projectRoot,
    targetPlatform: "windows-x64",
    buildResultPath: path.join(outputRoot, "build-result.json"),
    startingSourceFingerprint,
    metadata: {
      platform: "windows-x64",
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
      standalone_assets: standaloneAssets,
      installer_assets: installerAssets,
    },
  });
  process.stdout.write(`Windows x64 构建完成: ${outputRoot}\n`);
}

await main();
