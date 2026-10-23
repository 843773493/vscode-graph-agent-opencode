import { rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const DEMO_ROOT = path.resolve(fileURLToPath(new URL("../", import.meta.url)));
export const RUNTIME_ROOT = path.join(DEMO_ROOT, "runtime");

function runtimePathSegments(runtimeRelativePath) {
  return typeof runtimeRelativePath === "string" ? runtimeRelativePath.split(/[\\/]/) : [];
}

function assertRuntimeRelativePath(runtimeRelativePath) {
  const segments = runtimePathSegments(runtimeRelativePath);
  if (segments.length === 0 || segments.some((segment) => (
    segment === "."
    || segment === ".."
    || !/^[A-Za-z0-9._-]+$/.test(segment)
  ))) {
    throw new Error(`runtime 相对路径非法: ${runtimeRelativePath}`);
  }
}

export function resolveRuntimePath(runtimeRelativePath, runtimeBasePath = RUNTIME_ROOT) {
  assertRuntimeRelativePath(runtimeRelativePath);
  return path.join(path.resolve(runtimeBasePath), ...runtimePathSegments(runtimeRelativePath));
}

export async function resetDemoRuntime(runtimeRelativePath = "demo", runtimeBasePath = RUNTIME_ROOT) {
  const resolvedRuntimeBasePath = path.resolve(runtimeBasePath);
  const targetRuntimePath = resolveRuntimePath(runtimeRelativePath, resolvedRuntimeBasePath);
  const relativeRuntimePath = path.relative(resolvedRuntimeBasePath, targetRuntimePath);
  if (
    relativeRuntimePath === ""
    || relativeRuntimePath === ".."
    || relativeRuntimePath.startsWith(`..${path.sep}`)
    || path.isAbsolute(relativeRuntimePath)
  ) {
    throw new Error(`拒绝清理 runtime base 之外的路径: ${targetRuntimePath}`);
  }
  await rm(targetRuntimePath, { recursive: true, force: true });
}
