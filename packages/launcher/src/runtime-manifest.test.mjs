import { describe, expect, test } from "bun:test";
import path from "node:path";

import {
  resolveNodeExecutable,
  resolveRuntimeManifest,
  validateRuntimeManifest,
} from "./runtime-manifest.mjs";

const baseManifest = {
  schema_version: 1,
  distribution: "source-development",
  version: "0.1.0",
  python_executable: "../../.venv/bin/python",
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
    const manifestPath = "/tmp/boxteam/runtime/runtime-manifest.json";
    const manifest = resolveRuntimeManifest(manifestPath, baseManifest);

    expect(manifest.pythonExecutable).toBe(
      path.resolve("/tmp/boxteam/runtime", "../../.venv/bin/python"),
    );
    expect(manifest.applicationRoot).toBe("/tmp");
    expect(manifest.skillResources).toBe(
      path.resolve("/tmp/boxteam/runtime", "../../resources/skills"),
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
    const manifest = resolveRuntimeManifest("/tmp/runtime.json", {
      ...baseManifest,
      distribution: "npm",
    });

    expect(resolveNodeExecutable(manifest)).toBe(process.execPath);
  });

  test("standalone 使用 manifest 声明的 bundled Node", () => {
    const manifest = resolveRuntimeManifest("/tmp/runtime.json", {
      ...baseManifest,
      distribution: "standalone",
      node: {
        source: "bundled",
        executable: "node/bin/node",
      },
    });

    expect(resolveNodeExecutable(manifest)).toBe("/tmp/node/bin/node");
  });
});
