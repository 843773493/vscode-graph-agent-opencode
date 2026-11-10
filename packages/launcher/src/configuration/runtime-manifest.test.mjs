import { describe, expect, test } from "bun:test";
import Ajv2020 from "ajv/dist/2020.js";
import { readFileSync } from "node:fs";
import path from "node:path";

import {
  resolveNodeExecutable,
  resolveRuntimeManifest,
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

  test("npm 与 standalone 必须使用 bundled Node，source-* 保留 launcher Node", () => {
    for (const distribution of ["npm", "standalone"]) {
      expect(() => resolveRuntimeManifest(runtimeManifestPath, {
        ...baseManifest,
        distribution,
      })).toThrow("必须使用包内 bundled Node");
      const manifest = resolveRuntimeManifest(runtimeManifestPath, {
        ...baseManifest,
        distribution,
        node: { source: "bundled", executable: "node-runtime" },
      });
      expect(resolveNodeExecutable(manifest)).toBe(
        path.resolve(runtimeDirectory, "node-runtime"),
      );
    }
    expect(validateRuntimeManifest(baseManifest).node.source).toBe("launcher");
    expect(validateRuntimeManifest({ ...baseManifest, distribution: "source-installed" }).node.source).toBe("launcher");
  });

  test("JSON Schema 与发行物 Node 来源保持一致", () => {
    const schema = JSON.parse(readFileSync(
      new URL("../../runtime-manifest.schema.json", import.meta.url),
      "utf8",
    ));
    const validate = new Ajv2020().compile(schema);
    for (const distribution of ["source-development", "source-installed"]) {
      expect(validate({ ...baseManifest, distribution })).toBe(true);
    }
    for (const distribution of ["npm", "standalone"]) {
      expect(validate({
        ...baseManifest,
        distribution,
        node: { source: "bundled", executable: "runtime/node" },
      })).toBe(true);
      for (const node of [
        { source: "launcher", executable: null },
        { source: "bundled", executable: null },
        { source: "bundled", executable: " " },
      ]) {
        expect(validate({ ...baseManifest, distribution, node })).toBe(false);
      }
    }
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

});
