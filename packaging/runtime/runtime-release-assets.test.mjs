import { afterEach, describe, expect, test } from "bun:test";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";

import { BOXTEAM_VERSION } from "./versions.mjs";
import { stampReleasePackageManifest } from "./runtime-release-assets.mjs";

const temporaryRoots = [];

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

function temporaryPackage(packageJson) {
  const root = mkdtempSync(path.join(os.tmpdir(), "boxteam-package-manifest-"));
  temporaryRoots.push(root);
  writeFileSync(
    path.join(root, "package.json"),
    `${JSON.stringify(packageJson)}\n`,
    "utf8",
  );
  return root;
}

describe("发布包版本注入", () => {
  test("runtime 包使用根 package.json 版本", () => {
    const rootPackage = JSON.parse(
      readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
    );
    expect(BOXTEAM_VERSION).toBe(rootPackage.version);
    const root = temporaryPackage({
      name: "@boxteam/runtime-linux-x64",
      description: "runtime",
    });

    stampReleasePackageManifest(root);

    const packageJson = JSON.parse(
      readFileSync(path.join(root, "package.json"), "utf8"),
    );
    expect(packageJson.version).toBe(BOXTEAM_VERSION);
  });

  test("launcher 包使用根版本并绑定同版本平台 runtime", () => {
    const root = temporaryPackage({
      name: "boxteam",
      description: "launcher",
    });

    stampReleasePackageManifest(root, { launcher: true });

    const packageJson = JSON.parse(
      readFileSync(path.join(root, "package.json"), "utf8"),
    );
    expect(packageJson.version).toBe(BOXTEAM_VERSION);
    expect(packageJson.optionalDependencies).toEqual({
      "@boxteam/runtime-linux-x64": BOXTEAM_VERSION,
      "@boxteam/runtime-windows-x64": BOXTEAM_VERSION,
    });
  });
});
