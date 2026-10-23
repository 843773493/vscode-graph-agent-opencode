import { describe, expect, test } from "bun:test";
import path from "node:path";

import {
  discoverRuntimeManifestPath,
  runtimePackageName,
} from "./runtime-discovery.mjs";

const fixtureRoot = path.resolve(
  "out/tests/packages/launcher/src/runtime-discovery/workspace/项目 清单",
);
const explicitManifestPath = path.join(fixtureRoot, "boxteam", "runtime-manifest.json");
const linuxPackageManifestPath = path.join(fixtureRoot, "npm", "runtime-linux-x64", "runtime-manifest.json");
const windowsPackageManifestPath = path.join(fixtureRoot, "npm", "runtime-windows-x64", "runtime-manifest.json");

describe("runtime discovery", () => {
  test("显式 manifest 优先于平台包", () => {
    const resolved = discoverRuntimeManifestPath({
      environment: {
        BOXTEAM_RUNTIME_MANIFEST: explicitManifestPath,
      },
      platform: "darwin",
      architecture: "arm64",
      resolvePackage() {
        throw new Error("不应解析平台包");
      },
    });

    expect(resolved).toBe(explicitManifestPath);
  });

  test("Linux x64 选择组织平台包", () => {
    expect(runtimePackageName("linux", "x64")).toBe(
      "@boxteam/runtime-linux-x64",
    );
  });

  test("Windows x64 选择组织平台包", () => {
    expect(runtimePackageName("win32", "x64")).toBe(
      "@boxteam/runtime-windows-x64",
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
        return linuxPackageManifestPath;
      },
    });

    expect(requested).toEqual([
      "@boxteam/runtime-linux-x64/runtime-manifest.json",
    ]);
    expect(resolved).toBe(linuxPackageManifestPath);
  });

  test("不支持平台不回退系统 Python", () => {
    expect(() =>
      discoverRuntimeManifestPath({
        environment: {},
        platform: "darwin",
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

  test("Windows 平台包发现解析其 manifest export", () => {
    const requested = [];
    const resolved = discoverRuntimeManifestPath({
      environment: {},
      platform: "win32",
      architecture: "x64",
      resolvePackage(specifier) {
        requested.push(specifier);
        return windowsPackageManifestPath;
      },
    });

    expect(requested).toEqual([
      "@boxteam/runtime-windows-x64/runtime-manifest.json",
    ]);
    expect(resolved).toBe(windowsPackageManifestPath);
  });
});
