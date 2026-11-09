import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

const SUPPORTED_SCHEMA_VERSION = 1;
const DISTRIBUTIONS = new Set([
  "source-development",
  "source-installed",
  "npm",
  "standalone",
]);
const NODE_SOURCES = new Set(["launcher", "bundled"]);
const CONFIG_RESOURCE_KEYS = [
  "gateway_inline",
  "gateway_schema",
  "workspace_inline",
  "workspace_schema",
];

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
  const configResources = requireObject(
    manifest.config_resources,
    "runtime manifest.config_resources",
  );
  const unknownConfigResourceKeys = Object.keys(configResources).filter(
    (key) => !CONFIG_RESOURCE_KEYS.includes(key),
  );
  if (unknownConfigResourceKeys.length > 0) {
    throw new Error(
      `runtime manifest.config_resources 包含未知字段: ${unknownConfigResourceKeys.join(", ")}`,
    );
  }
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
  if ((manifest.distribution === "npm" || manifest.distribution === "standalone") && node.source !== "bundled") {
    throw new Error(`${manifest.distribution} 发行物必须使用包内 bundled Node`);
  }
  let extensionHost = null;
  if (manifest.extension_host !== null && manifest.extension_host !== undefined) {
    const value = requireObject(manifest.extension_host, "runtime manifest.extension_host");
    const unknownKeys = Object.keys(value).filter(
      (key) => !["node_executable", "component_root"].includes(key),
    );
    if (unknownKeys.length > 0) {
      throw new Error(`runtime manifest.extension_host 包含未知字段: ${unknownKeys.join(", ")}`);
    }
    extensionHost = Object.freeze({
      nodeExecutable: requireString(value.node_executable, "runtime manifest.extension_host.node_executable"),
      componentRoot: requireString(value.component_root, "runtime manifest.extension_host.component_root"),
    });
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
    configResources: Object.freeze(
      Object.fromEntries(
        CONFIG_RESOURCE_KEYS.map((key) => [
          key,
          requireString(
            configResources[key],
            `runtime manifest.config_resources.${key}`,
          ),
        ]),
      ),
    ),
    skillResources: requireString(
      manifest.skill_resources,
      "runtime manifest.skill_resources",
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
    extensionHost,
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
    configResources: Object.freeze(
      Object.fromEntries(
        Object.entries(manifest.configResources).map(([key, value]) => [
          key,
          resolveResource(runtimeRoot, value),
        ]),
      ),
    ),
    skillResources: resolveResource(runtimeRoot, manifest.skillResources),
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
    extensionHost:
      manifest.extensionHost === null
        ? null
        : Object.freeze({
            nodeExecutable: resolveResource(runtimeRoot, manifest.extensionHost.nodeExecutable),
            componentRoot: resolveResource(runtimeRoot, manifest.extensionHost.componentRoot),
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
    ...Object.entries(manifest.configResources).map(([name, resourcePath]) => [
      `配置资源 ${name}`,
      resourcePath,
    ]),
    ["共享 Skill 资源", manifest.skillResources],
    ...(manifest.webAssets === null ? [] : [["Web UI", manifest.webAssets]]),
    ...(manifest.chromiumExecutable === null
      ? []
      : [["Chromium", manifest.chromiumExecutable]]),
    ...(manifest.node.source === "bundled"
      ? [["Bundled Node", manifest.node.executable]]
      : []),
  ]) {
    if (!existsSync(resourcePath)) {
      throw new Error(
        `${label}资源不存在: ${resourcePath} ` +
          `(distribution=${manifest.distribution}, manifest=${manifest.manifestPath})`,
      );
    }
  }
  if (manifest.extensionHost !== null) {
    const root = manifest.extensionHost.componentRoot;
    const extensionHostResources = [
      ["Extension Host Node", manifest.extensionHost.nodeExecutable, "file"],
      ["Extension Host component root", root, "directory"],
      ...["extensionHostProcess.mjs", "version.json", "package.json", "package-lock.json"].map((name) => [
        `Extension Host component ${name}`,
        path.join(root, name),
        "file",
      ]),
    ];
    for (const [label, resourcePath, type] of extensionHostResources) {
      if (!existsSync(resourcePath)) {
        throw new Error(
          `${label}资源不存在: ${resourcePath} ` +
            `(distribution=${manifest.distribution}, manifest=${manifest.manifestPath})`,
        );
      }
      const stat = statSync(resourcePath);
      if (type === "file" ? !stat.isFile() : !stat.isDirectory()) {
        throw new Error(`${label}资源类型无效: ${resourcePath}`);
      }
    }
  }
}

export function resolveNodeExecutable(manifest) {
  if (manifest.node.source === "launcher") {
    return process.execPath;
  }
  return manifest.node.executable;
}
