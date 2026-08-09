import { createHash } from "node:crypto";
import {
  cpSync,
  createReadStream,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import {
  BOXTEAM_GITHUB_REPOSITORY,
  BOXTEAM_VERSION,
  RUNTIME_DOWNLOADER_DEPENDENCIES,
} from "./versions.mjs";

const POSTINSTALL_SCRIPT = "runtime-postinstall.mjs";

export function runtimeAssetFilename(platform) {
  return `boxteam-runtime-${platform}-${BOXTEAM_VERSION}.tgz`;
}

export function runtimeAssetUrl(platform) {
  return (
    `https://github.com/${BOXTEAM_GITHUB_REPOSITORY}/releases/download/` +
    `v${BOXTEAM_VERSION}/${runtimeAssetFilename(platform)}`
  );
}

export async function sha256File(filePath) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) {
    hash.update(chunk);
  }
  return hash.digest("hex");
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
    path.join(projectRoot, "packaging", "runtime", POSTINSTALL_SCRIPT),
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
