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
  if (!Array.isArray(manifest.bundled_plugins)) throw new TypeError("runtime manifest.bundled_plugins 必须是数组");
  const bundledPlugins = manifest.bundled_plugins.map(value => {
    const entry = requireObject(value, "runtime manifest.bundled_plugins entry");
    const sources = ["directory", "archive"].filter(key => Object.hasOwn(entry, key));
    if (sources.length !== 1) throw new TypeError("内置插件必须且只能提供 directory 或 archive");
    const source = sources[0];
    return Object.freeze({ package_id: requireString(entry.package_id, "bundled plugin package_id"),
      package_version: requireString(entry.package_version, "bundled plugin package_version"),
      [source]: requireString(entry[source], `bundled plugin ${source}`) });
  });
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
    bundledPlugins: Object.freeze(bundledPlugins),
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
    configResources: Object.freeze(
      Object.fromEntries(
        Object.entries(manifest.configResources).map(([key, value]) => [
          key,
          resolveResource(runtimeRoot, value),
        ]),
      ),
    ),
    skillResources: resolveResource(runtimeRoot, manifest.skillResources),
    bundledPlugins: Object.freeze(manifest.bundledPlugins.map(entry => Object.freeze({ ...entry,
      [entry.directory === undefined ? "archive" : "directory"]: resolveResource(runtimeRoot, entry.directory ?? entry.archive) }))),
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
    ...Object.entries(manifest.configResources).map(([name, resourcePath]) => [
      `配置资源 ${name}`,
      resourcePath,
    ]),
    ["共享 Skill 资源", manifest.skillResources],
    ...manifest.bundledPlugins.map(entry => [`内置插件 ${entry.package_id}`, entry.directory ?? entry.archive]),
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
  for (const entry of manifest.bundledPlugins) {
    const resource = statSync(entry.directory ?? entry.archive);
    if (entry.directory !== undefined ? !resource.isDirectory() : !resource.isFile()) {
      throw new TypeError(`内置插件来源类型不符: ${entry.directory ?? entry.archive}`);
    }
  }
}

export function resolveNodeExecutable(manifest) {
  if (manifest.node.source === "launcher") {
    return process.execPath;
  }
  return manifest.node.executable;
}
