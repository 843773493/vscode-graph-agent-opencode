import { createRequire } from "node:module";
import path from "node:path";

import {
  assertRuntimeResources,
  loadRuntimeManifest,
  resolveNodeExecutable,
} from "./runtime-manifest.mjs";

const require = createRequire(import.meta.url);

const PLATFORM_RUNTIME_PACKAGES = new Map([
  ["linux:x64", "@boxteam/runtime-linux-x64"],
]);

function platformKey(platform, architecture) {
  return `${platform}:${architecture}`;
}

export function runtimePackageName(
  platform = process.platform,
  architecture = process.arch,
) {
  return PLATFORM_RUNTIME_PACKAGES.get(platformKey(platform, architecture)) ?? null;
}

export function discoverRuntimeManifestPath({
  environment = process.env,
  platform = process.platform,
  architecture = process.arch,
  resolvePackage = (specifier) => require.resolve(specifier),
} = {}) {
  const explicitPath = environment.BOXTEAM_RUNTIME_MANIFEST?.trim();
  if (explicitPath) {
    return path.resolve(explicitPath);
  }

  const packageName = runtimePackageName(platform, architecture);
  if (packageName === null) {
    throw new Error(
      `BoxTeam 暂不支持当前平台: os=${platform} cpu=${architecture}。` +
        "未找到匹配的内置运行时，且不会回退到系统 Python。",
    );
  }
  try {
    return resolvePackage(`${packageName}/runtime-manifest.json`);
  } catch (error) {
    throw new Error(
      `缺少 BoxTeam 平台运行时 ${packageName}: ${
        error instanceof Error ? error.message : String(error)
      }。请重新安装 boxteam，Launcher 不会回退到系统 Python。`,
    );
  }
}

export function discoverRuntime(options = {}) {
  const manifestPath = discoverRuntimeManifestPath(options);
  const manifest = loadRuntimeManifest(manifestPath);
  assertRuntimeResources(manifest);
  return Object.freeze({
    ...manifest,
    nodeExecutable: resolveNodeExecutable(manifest),
  });
}
