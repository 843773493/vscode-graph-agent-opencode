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
  });

  test("拒绝未知 schema", () => {
    expect(() =>
      validateRuntimeManifest({ ...baseManifest, schema_version: 2 }),
    ).toThrow("不支持的 runtime manifest");
  });

  test("npm launcher Node 使用当前 Node", () => {
    const manifest = resolveRuntimeManifest("/tmp/runtime.json", {
      ...baseManifest,
      distribution: "npm",
    });

    expect(resolveNodeExecutable(manifest)).toBe(process.execPath);
  });

  test("bundled Node 保留为明确的未来扩展点", () => {
    const manifest = resolveRuntimeManifest("/tmp/runtime.json", {
      ...baseManifest,
      node: {
        source: "bundled",
        executable: "node/bin/node",
      },
    });

    expect(() => resolveNodeExecutable(manifest)).toThrow(
      "尚未实现 bundled Node",
    );
  });
});
