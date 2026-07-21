import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const SUPPORTED_SCHEMA_VERSION = 1;
const DISTRIBUTIONS = new Set([
  "source-development",
  "source-installed",
  "npm",
]);
const NODE_SOURCES = new Set(["launcher", "bundled"]);

function requireObject(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} 必须是对象`);
  }
  return value;
}

function requireString(value, label) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new TypeError(`${label} 必须是非空字符串`);
  }
  return value;
}

function optionalString(value, label) {
  if (value === null || value === undefined) return null;
  return requireString(value, label);
}

export function validateRuntimeManifest(value) {
  const manifest = requireObject(value, "runtime manifest");
  if (manifest.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    throw new Error(
      `不支持的 runtime manifest schema_version: ${String(manifest.schema_version)}，` +
        `当前仅支持 ${SUPPORTED_SCHEMA_VERSION}`,
    );
  }
  if (!DISTRIBUTIONS.has(manifest.distribution)) {
    throw new Error(`未知 runtime distribution: ${String(manifest.distribution)}`);
  }
  const node = requireObject(manifest.node, "runtime manifest.node");
  if (!NODE_SOURCES.has(node.source)) {
    throw new Error(`未知 Node runtime source: ${String(node.source)}`);
  }
  const nodeExecutable = optionalString(
    node.executable,
    "runtime manifest.node.executable",
  );
  if (node.source === "launcher" && nodeExecutable !== null) {
    throw new Error("node.source=launcher 时 executable 必须为 null");
  }
  if (node.source === "bundled" && nodeExecutable === null) {
    throw new Error("node.source=bundled 时必须提供 executable");
  }
  return Object.freeze({
    schemaVersion: manifest.schema_version,
    distribution: manifest.distribution,
    version: requireString(manifest.version, "runtime manifest.version"),
    pythonExecutable: requireString(
      manifest.python_executable,
      "runtime manifest.python_executable",
    ),
    applicationRoot: requireString(
      manifest.application_root,
      "runtime manifest.application_root",
    ),
    webAssets: optionalString(manifest.web_assets, "runtime manifest.web_assets"),
    chromiumExecutable: optionalString(
      manifest.chromium_executable,
      "runtime manifest.chromium_executable",
    ),
    node: Object.freeze({
      source: node.source,
      executable: nodeExecutable,
    }),
  });
}

function resolveResource(runtimeRoot, value) {
  return path.isAbsolute(value) ? path.normalize(value) : path.resolve(runtimeRoot, value);
}

export function resolveRuntimeManifest(manifestPath, value) {
  const resolvedManifestPath = path.resolve(manifestPath);
  const runtimeRoot = path.dirname(resolvedManifestPath);
  const manifest = validateRuntimeManifest(value);
  return Object.freeze({
    ...manifest,
    manifestPath: resolvedManifestPath,
    runtimeRoot,
    pythonExecutable: resolveResource(runtimeRoot, manifest.pythonExecutable),
    applicationRoot: resolveResource(runtimeRoot, manifest.applicationRoot),
    webAssets:
      manifest.webAssets === null
        ? null
        : resolveResource(runtimeRoot, manifest.webAssets),
    chromiumExecutable:
      manifest.chromiumExecutable === null
        ? null
        : resolveResource(runtimeRoot, manifest.chromiumExecutable),
    node: Object.freeze({
      ...manifest.node,
      executable:
        manifest.node.executable === null
          ? null
          : resolveResource(runtimeRoot, manifest.node.executable),
    }),
  });
}

export function loadRuntimeManifest(manifestPath) {
  const resolvedPath = path.resolve(manifestPath);
  if (!existsSync(resolvedPath)) {
    throw new Error(`runtime manifest 不存在: ${resolvedPath}`);
  }
  let value;
  try {
    value = JSON.parse(readFileSync(resolvedPath, "utf8"));
  } catch (error) {
    throw new Error(
      `解析 runtime manifest 失败: ${resolvedPath}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
  return resolveRuntimeManifest(resolvedPath, value);
}

export function assertRuntimeResources(manifest) {
  for (const [label, resourcePath] of [
    ["Python", manifest.pythonExecutable],
    ["应用根目录", manifest.applicationRoot],
    ...(manifest.webAssets === null ? [] : [["Web UI", manifest.webAssets]]),
    ...(manifest.chromiumExecutable === null
      ? []
      : [["Chromium", manifest.chromiumExecutable]]),
  ]) {
    if (!existsSync(resourcePath)) {
      throw new Error(
        `${label}资源不存在: ${resourcePath} ` +
          `(distribution=${manifest.distribution}, manifest=${manifest.manifestPath})`,
      );
    }
  }
}

export function resolveNodeExecutable(manifest) {
  if (manifest.node.source === "launcher") {
    return process.execPath;
  }
  // TODO: Windows .exe 与 Linux 独立安装程序落地时，在这里启用 manifest
  // 声明的 bundled Node；npm 发行版不得回退搜索 PATH。
  throw new Error(
    `当前发行版尚未实现 bundled Node: ${manifest.node.executable ?? "未声明"}`,
  );
}
