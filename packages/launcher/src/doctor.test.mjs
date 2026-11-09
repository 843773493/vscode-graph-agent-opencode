import { describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import path from "node:path";

import { buildDoctorReport } from "./doctor.mjs";

function doctorRuntime(nodeExecutable) {
  return {
    distribution: "npm",
    version: "test-version",
    manifestPath: path.resolve("runtime-manifest-fixture/runtime-manifest.json"),
    runtimeRoot: path.resolve("runtime-manifest-fixture"),
    pythonExecutable: path.resolve("runtime-manifest-fixture/python"),
    node: { source: "bundled", executable: nodeExecutable },
    nodeExecutable,
    webAssets: null,
    chromiumExecutable: null,
  };
}

describe("doctor Node 诊断", () => {
  test("执行 manifest 解析出的 Node --version", () => {
    const report = buildDoctorReport({
      runtime: doctorRuntime(process.execPath),
      boxteamHome: path.resolve("out/tests/packages/launcher/src/doctor/workspace/home"),
    });

    expect(report.node.version).toBe(
      execFileSync(process.execPath, ["--version"], { encoding: "utf8" }).trim(),
    );
    expect(report.node.executable).toBe(process.execPath);
  });

  test("manifest Node 无法运行时明确失败，不回报 Launcher 版本", () => {
    const missingNode = path.resolve("out/tests/packages/launcher/src/doctor/workspace/missing-node");

    expect(() => buildDoctorReport({
      runtime: doctorRuntime(missingNode),
      boxteamHome: path.resolve("out/tests/packages/launcher/src/doctor/workspace/missing-home"),
    })).toThrow(missingNode);
  });
});
