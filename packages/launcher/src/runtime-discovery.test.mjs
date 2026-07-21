import { describe, expect, test } from "bun:test";

import {
  discoverRuntimeManifestPath,
  runtimePackageName,
} from "./runtime-discovery.mjs";

describe("runtime discovery", () => {
  test("显式 manifest 优先于平台包", () => {
    const resolved = discoverRuntimeManifestPath({
      environment: {
        BOXTEAM_RUNTIME_MANIFEST: "/tmp/boxteam/runtime-manifest.json",
      },
      platform: "darwin",
      architecture: "arm64",
      resolvePackage() {
        throw new Error("不应解析平台包");
      },
    });

    expect(resolved).toBe("/tmp/boxteam/runtime-manifest.json");
  });

  test("Linux x64 选择组织平台包", () => {
    expect(runtimePackageName("linux", "x64")).toBe(
      "@boxteam/runtime-linux-x64",
    );
  });

  test("平台包发现解析其 manifest export", () => {
    const requested = [];
    const resolved = discoverRuntimeManifestPath({
      environment: {},
      platform: "linux",
      architecture: "x64",
      resolvePackage(specifier) {
        requested.push(specifier);
        return "/npm/runtime-linux-x64/runtime-manifest.json";
      },
    });

    expect(requested).toEqual([
      "@boxteam/runtime-linux-x64/runtime-manifest.json",
    ]);
    expect(resolved).toBe("/npm/runtime-linux-x64/runtime-manifest.json");
  });

  test("不支持平台不回退系统 Python", () => {
    expect(() =>
      discoverRuntimeManifestPath({
        environment: {},
        platform: "win32",
        architecture: "x64",
      }),
    ).toThrow("不会回退到系统 Python");
  });

  test("平台包缺失给出重新安装建议", () => {
    expect(() =>
      discoverRuntimeManifestPath({
        environment: {},
        platform: "linux",
        architecture: "x64",
        resolvePackage() {
          throw new Error("MODULE_NOT_FOUND");
        },
      }),
    ).toThrow("@boxteam/runtime-linux-x64");
  });
});
