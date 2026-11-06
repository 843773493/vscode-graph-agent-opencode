import { describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import path from "node:path";

import {
  resolveNodeExecutable,
  resolveRuntimeManifest,
  assertRuntimeResources,
  validateRuntimeManifest,
} from "./runtime-manifest.mjs";

const runtimeManifestPath = path.resolve(
  "runtime-manifest-fixture",
  "runtime-manifest.json",
);
const runtimeDirectory = path.dirname(runtimeManifestPath);

const baseManifest = {
  schema_version: 1,
  distribution: "source-development",
  version: "test-version",
  python_executable: "../../runtime/python",
  application_root: "../..",
  config_resources: {
    gateway_inline: "../../configs/gateway_inline.jsonc",
    gateway_schema: "../../configs/gateway_schema.jsonc",
    workspace_inline: "../../configs/workspace_inline.jsonc",
    workspace_schema: "../../configs/workspace_schema.jsonc",
  },
  skill_resources: "../../resources/skills",
  web_assets: null,
  chromium_executable: null,
  node: {
    source: "launcher",
    executable: null,
  },
};

describe("runtime manifest", () => {
  test("校验 development manifest", () => {
    const manifest = validateRuntimeManifest(baseManifest);

    expect(manifest.distribution).toBe("source-development");
    expect(manifest.node.source).toBe("launcher");
  });

  test("相对资源以 manifest 目录解析", () => {
    const manifest = resolveRuntimeManifest(runtimeManifestPath, baseManifest);

    expect(manifest.pythonExecutable).toBe(
      path.resolve(runtimeDirectory, "../../runtime/python"),
    );
    expect(manifest.applicationRoot).toBe(path.resolve(runtimeDirectory, "../.."));
    expect(manifest.skillResources).toBe(
      path.resolve(runtimeDirectory, "../../resources/skills"),
    );
  });

  test("拒绝未知 schema", () => {
    expect(() =>
      validateRuntimeManifest({ ...baseManifest, schema_version: 2 }),
    ).toThrow("不支持的 runtime manifest");
  });

  test("拒绝未声明的配置资源域", () => {
    expect(() =>
      validateRuntimeManifest({
        ...baseManifest,
        config_resources: {
          ...baseManifest.config_resources,
          legacy_combined: "../../configs/boxteam.jsonc",
        },
      }),
    ).toThrow("包含未知字段");
  });

  test("npm launcher Node 使用当前 Node", () => {
    const manifest = resolveRuntimeManifest(runtimeManifestPath, {
      ...baseManifest,
      distribution: "npm",
    });

    expect(resolveNodeExecutable(manifest)).toBe(process.execPath);
  });

  test("standalone 使用 manifest 声明的 bundled Node", () => {
    const manifest = resolveRuntimeManifest(runtimeManifestPath, {
      ...baseManifest,
      distribution: "standalone",
      node: {
        source: "bundled",
        executable: "node-runtime",
      },
    });

    expect(resolveNodeExecutable(manifest)).toBe(
      path.resolve(runtimeDirectory, "node-runtime"),
    );
  });

  test("extension_host 可缺省或为 null，并解析成 manifest 所在目录下的显式资源", () => {
    expect(validateRuntimeManifest(baseManifest).extensionHost).toBeNull();
    expect(validateRuntimeManifest({ ...baseManifest, extension_host: null }).extensionHost).toBeNull();
    const manifest = resolveRuntimeManifest(runtimeManifestPath, {
      ...baseManifest,
      extension_host: { node_executable: "host/node", component_root: "host/component" },
    });
    expect(manifest.extensionHost).toEqual({
      nodeExecutable: path.resolve(runtimeDirectory, "host/node"),
      componentRoot: path.resolve(runtimeDirectory, "host/component"),
    });
  });

  test("extension_host 拒绝部分字段、空路径与未知成员", () => {
    for (const extension_host of [
      {},
      { node_executable: "node" },
      { component_root: "component" },
      { node_executable: " ", component_root: "component" },
      { node_executable: "node", component_root: " " },
      { node_executable: "node", component_root: "component", fallback: "node" },
    ]) {
      expect(() => validateRuntimeManifest({ ...baseManifest, extension_host })).toThrow();
    }
    expect(() => validateRuntimeManifest({ ...baseManifest, extension_host: "component" })).toThrow("必须是对象");
  });

  test("显式 extension_host 必须包含 Node 文件、组件目录和固定清单资源", () => {
    const workspace = path.resolve("out/tests/packages/launcher/src/configuration/runtime-manifest/workspace");
    mkdirSync(workspace, { recursive: true });
    const root = mkdtempSync(path.join(workspace, "extension-host-"));
    const resources = path.join(root, "resources");
    mkdirSync(resources);
    const pythonExecutable = path.join(resources, "python");
    const applicationRoot = path.join(resources, "application");
    const skillResources = path.join(resources, "skills");
    mkdirSync(applicationRoot);
    mkdirSync(skillResources);
    writeFileSync(pythonExecutable, "python");
    const configResources = Object.fromEntries(Object.keys(baseManifest.config_resources).map((name) => {
      const resource = path.join(resources, `${name}.jsonc`);
      writeFileSync(resource, "{}");
      return [name, resource];
    }));
    const nodeExecutable = path.join(root, "node");
    const componentRoot = path.join(root, "component");
    mkdirSync(componentRoot);
    writeFileSync(nodeExecutable, "fixture node");
    for (const name of ["extensionHostProcess.mjs", "version.json", "package.json", "package-lock.json"]) {
      writeFileSync(path.join(componentRoot, name), "{}");
    }
    const manifest = resolveRuntimeManifest(path.join(root, "runtime-manifest.json"), {
      ...baseManifest,
      python_executable: pythonExecutable,
      application_root: applicationRoot,
      config_resources: configResources,
      skill_resources: skillResources,
      extension_host: { node_executable: nodeExecutable, component_root: componentRoot },
    });
    expect(() => assertRuntimeResources(manifest)).not.toThrow();
    const missingManifest = resolveRuntimeManifest(path.join(root, "runtime-manifest.json"), {
      ...baseManifest,
      python_executable: pythonExecutable,
      application_root: applicationRoot,
      config_resources: configResources,
      skill_resources: skillResources,
      extension_host: { node_executable: nodeExecutable, component_root: path.join(root, "missing") },
    });
    expect(() => assertRuntimeResources(missingManifest)).toThrow("Extension Host component root资源不存在");
  });
});
