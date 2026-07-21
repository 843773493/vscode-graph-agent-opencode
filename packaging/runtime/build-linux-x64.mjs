import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import {
  BOXTEAM_VERSION,
  NODE_RUNTIME_DEPENDENCIES,
  PYTHON_RUNTIME,
} from "./versions.mjs";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const outputRoot = path.join(projectRoot, "out", "packaging", "linux-x64");
const downloadRoot = path.join(outputRoot, "downloads");
const stageRoot = path.join(outputRoot, "stage");
const runtimePackageRoot = path.join(
  stageRoot,
  "runtime-linux-x64",
  "package",
);
const launcherPackageRoot = path.join(stageRoot, "launcher", "package");
const tarballRoot = path.join(outputRoot, "tarballs");

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

async function downloadPinnedPython() {
  mkdirSync(downloadRoot, { recursive: true });
  const archivePath = path.join(downloadRoot, PYTHON_RUNTIME.archive);
  if (!existsSync(archivePath)) {
    const response = await fetch(PYTHON_RUNTIME.url);
    if (!response.ok) {
      throw new Error(
        `下载 Python 运行时失败: HTTP ${response.status} ${response.statusText}`,
      );
    }
    writeFileSync(archivePath, Buffer.from(await response.arrayBuffer()));
  }
  const digest = createHash("sha256")
    .update(readFileSync(archivePath))
    .digest("hex");
  if (digest !== PYTHON_RUNTIME.sha256) {
    throw new Error(
      `Python 运行时摘要不匹配: expected=${PYTHON_RUNTIME.sha256} actual=${digest}`,
    );
  }
  return archivePath;
}

function copyApplicationSources(applicationRoot) {
  const copyOptions = {
    recursive: true,
    filter(source) {
      const relative = path.relative(projectRoot, source);
      return (
        !relative.split(path.sep).includes("__pycache__") &&
        !relative.endsWith(".pyc")
      );
    },
  };
  cpSync(path.join(projectRoot, "app"), path.join(applicationRoot, "app"), copyOptions);
  cpSync(
    path.join(projectRoot, "configs"),
    path.join(applicationRoot, "configs"),
    copyOptions,
  );
  for (const service of ["terminal", "browser"]) {
    cpSync(
      path.join(projectRoot, "src", service, "server"),
      path.join(applicationRoot, "src", service, "server"),
      copyOptions,
    );
  }
  cpSync(
    path.join(projectRoot, "pyproject.toml"),
    path.join(applicationRoot, "pyproject.toml"),
  );
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
  run("bun", ["install", "--production", "--exact"], {
    cwd: applicationRoot,
    env: {
      ...process.env,
      PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1",
    },
  });
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
  cpSync(chromiumCacheRoot, chromiumRoot, { recursive: true });
  const packagedExecutable = path.join(
    chromiumRoot,
    path.relative(chromiumCacheRoot, executable),
  );
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
        chromium: "Chromium 项目随附许可文件，构建产物未裁剪浏览器资源",
      },
      null,
      2,
    )}\n`,
  );
}

function stageNpmPackages() {
  cpSync(
    path.join(projectRoot, "packages", "runtime-linux-x64", "package.json"),
    path.join(runtimePackageRoot, "package.json"),
  );
  cpSync(
    path.join(projectRoot, "packages", "launcher"),
    launcherPackageRoot,
    {
      recursive: true,
      filter(source) {
        return !source.split(path.sep).includes("node_modules");
      },
    },
  );
}

function npmPack(packageRoot) {
  const filename = run(
    "npm",
    ["pack", "--silent", "--pack-destination", tarballRoot],
    { cwd: packageRoot, capture: true },
  );
  const outputLines = filename.split(/\r?\n/).filter(Boolean);
  if (outputLines.length !== 1 || !outputLines[0].endsWith(".tgz")) {
    throw new Error(`npm pack 返回未知结果: ${filename}`);
  }
  return path.join(tarballRoot, outputLines[0]);
}

function directoryBytes(root) {
  let bytes = 0;
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    bytes += entry.isDirectory() ? directoryBytes(target) : statSync(target).size;
  }
  return bytes;
}

function writeSizeReport(tarballs) {
  const components = {};
  for (const name of ["python", "application", "web", "chromium"]) {
    components[name] = directoryBytes(path.join(runtimePackageRoot, name));
  }
  const report = {
    components,
    tarballs: Object.fromEntries(
      tarballs.map((tarball) => [path.basename(tarball), statSync(tarball).size]),
    ),
  };
  writeFileSync(
    path.join(outputRoot, "size-report.json"),
    `${JSON.stringify(report, null, 2)}\n`,
  );
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

async function main() {
  if (process.platform !== "linux" || process.arch !== "x64") {
    throw new Error(
      `linux-x64 构建器不支持当前平台: ${process.platform}-${process.arch}`,
    );
  }
  rmSync(stageRoot, { recursive: true, force: true });
  rmSync(tarballRoot, { recursive: true, force: true });
  mkdirSync(runtimePackageRoot, { recursive: true });
  mkdirSync(tarballRoot, { recursive: true });

  const pythonArchive = await downloadPinnedPython();
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
  copyApplicationSources(applicationRoot);
  installPythonDependencies(pythonExecutable);
  installNodeDependencies(applicationRoot);

  run("bun", ["run", "build"], {
    cwd: path.join(projectRoot, "src", "web"),
  });
  cpSync(
    path.join(projectRoot, "src", "web", "dist"),
    path.join(runtimePackageRoot, "web"),
    { recursive: true },
  );
  const chromiumExecutable = installChromium(
    applicationRoot,
    path.join(runtimePackageRoot, "chromium"),
  );
  writeRuntimeMetadata({ chromiumExecutable });
  stageNpmPackages();

  const tarballs = [
    npmPack(runtimePackageRoot),
    npmPack(launcherPackageRoot),
  ];
  writeSizeReport(tarballs);
  writeFileSync(
    path.join(outputRoot, "build-result.json"),
    `${JSON.stringify({ tarballs }, null, 2)}\n`,
  );
  process.stdout.write(`构建完成: ${outputRoot}\n`);
}

await main();
