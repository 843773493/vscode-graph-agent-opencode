import { createHash } from "node:crypto";
import {
  cpSync,
  createReadStream,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import {
  BOXTEAM_GITHUB_REPOSITORY,
  BOXTEAM_VERSION,
  RUNTIME_DOWNLOADER_DEPENDENCIES,
} from "./versions.mjs";

const POSTINSTALL_SCRIPT = "runtime-postinstall.mjs";
const NODE_MODULES_DIRECTORY_PATH = `${path.sep}node_modules${path.sep}`;

export function runtimeAssetFilename(platform) {
  return `boxteam-runtime-${platform}-${BOXTEAM_VERSION}.tgz`;
}

export function runtimeAssetUrl(platform) {
  return (
    `https://github.com/${BOXTEAM_GITHUB_REPOSITORY}/releases/download/` +
    `v${BOXTEAM_VERSION}/${runtimeAssetFilename(platform)}`
  );
}

export function stampReleasePackageManifest(packageRoot, { launcher = false } = {}) {
  const packageJsonPath = path.join(packageRoot, "package.json");
  const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8"));
  packageJson.version = BOXTEAM_VERSION;
  if (launcher) {
    delete packageJson.scripts?.test;
    packageJson.optionalDependencies = {
      "@boxteam/runtime-linux-x64": BOXTEAM_VERSION,
      "@boxteam/runtime-windows-x64": BOXTEAM_VERSION,
    };
  }
  writeFileSync(
    packageJsonPath,
    `${JSON.stringify(packageJson, null, 2)}\n`,
  );
}

export function stageRuntimeAndLauncherPackages({ projectRoot, platform, runtimePackageRoot, launcherPackageRoot }) {
  cpSync(
    path.join(projectRoot, "packages", `runtime-${platform}`, "package.json"),
    path.join(runtimePackageRoot, "package.json"),
  );
  cpSync(path.join(projectRoot, "packages", "launcher"), launcherPackageRoot, {
    recursive: true,
    filter(source) {
      if (`${path.sep}${source}${path.sep}`.includes(NODE_MODULES_DIRECTORY_PATH)) {
        return false;
      }
      const name = path.basename(source);
      // 只排除普通开发文件；同名目录和链接仍保持原复制语义。
      return !(name === "AGENTS.md" || name.endsWith(".test.mjs")) || !lstatSync(source).isFile();
    },
  });
  stampReleasePackageManifest(runtimePackageRoot);
  stampReleasePackageManifest(launcherPackageRoot, { launcher: true });
}

export async function sha256File(filePath) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) {
    hash.update(chunk);
  }
  return hash.digest("hex");
}

/** 累加非目录条目的stat长度，沿用链接目标长度；不表示磁盘实际占用。 */
function sumDirectoryEntryBytes(root) {
  let bytes = 0;
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    bytes += entry.isDirectory()
      ? sumDirectoryEntryBytes(target)
      : statSync(target).size;
  }
  return bytes;
}

export function writeRuntimeSizeReport({ runtimePackageRoot, outputRoot, npmTarballs, assetGroups }) {
  const components = {};
  for (const name of ["python", "application", "web", "chromium"]) {
    components[name] = sumDirectoryEntryBytes(path.join(runtimePackageRoot, name));
  }
  const report = {
    components,
    npm_tarballs: Object.fromEntries(
      npmTarballs.map((tarball) => [path.basename(tarball), statSync(tarball).size]),
    ),
  };
  for (const [name, assets] of Object.entries(assetGroups)) {
    report[name] = Object.fromEntries(
      assets.map((asset) => [asset.filename, statSync(asset.path).size]),
    );
  }
  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  writeFileSync(path.join(outputRoot, "size-report.json"), serialized);
  process.stdout.write(serialized);
}

export async function stageRuntimeDownloaderPackage({
  projectRoot,
  sourcePackagePath,
  stageRoot,
  platform,
  releaseAssetPath,
}) {
  const packageRoot = path.join(stageRoot, "package");
  rmSync(packageRoot, { recursive: true, force: true });
  mkdirSync(packageRoot, { recursive: true });

  const packageJson = JSON.parse(readFileSync(sourcePackagePath, "utf8"));
  packageJson.version = BOXTEAM_VERSION;
  packageJson.description = `${packageJson.description}（GitHub Release 下载器）`;
  packageJson.dependencies = RUNTIME_DOWNLOADER_DEPENDENCIES;
  packageJson.scripts = { postinstall: `node ${POSTINSTALL_SCRIPT}` };
  packageJson.files = [POSTINSTALL_SCRIPT];
  packageJson.boxteam_runtime = {
    release_asset: {
      filename: runtimeAssetFilename(platform),
      url: runtimeAssetUrl(platform),
      sha256: await sha256File(releaseAssetPath),
    },
  };

  cpSync(
    path.join(projectRoot, "scripts", "install", POSTINSTALL_SCRIPT),
    path.join(packageRoot, POSTINSTALL_SCRIPT),
  );
  writeFileSync(
    path.join(packageRoot, "package.json"),
    `${JSON.stringify(packageJson, null, 2)}\n`,
  );
  return Object.freeze({
    packageRoot,
    releaseAsset: Object.freeze(packageJson.boxteam_runtime.release_asset),
  });
}
