import { describe, expect, test } from "bun:test";
import path from "node:path";

import { BOXTEAM_VERSION } from "../../packaging/runtime/versions.mjs";
import {
  buildReleaseInstallCommand,
  parseInstallArguments,
  runReleaseInstall,
} from "./install-release.mjs";

// 这里只组装路径与命令，不创建安装目录或执行npm；真实路径遵循运行主机的规则。
const fixtureRoot = path.resolve(process.cwd(), "out/tests/scripts/install/install-release/workspace");
const fixtureProjectRoot = path.join(fixtureRoot, "源码 工作区");
const fixtureInstallPrefix = path.join(fixtureRoot, "隔离 安装");

describe("发布版安装入口", () => {
  test("默认从当前源码的 Linux 本地 tarball 安装", () => {
    const launcherTarball = path.join(fixtureProjectRoot, "out", "packaging", "linux-x64", "tarballs", `boxteam-${BOXTEAM_VERSION}.tgz`);
    const runtimeTarball = path.join(fixtureProjectRoot, "out", "packaging", "linux-x64", "release-assets", `boxteam-runtime-linux-x64-${BOXTEAM_VERSION}.tgz`);
    const command = buildReleaseInstallCommand({
      platform: "linux",
      architecture: "x64",
      projectRoot: fixtureProjectRoot,
    });

    expect(command).toEqual({
      command: "npm",
      args: [
        "install",
        "--global",
        "--no-audit",
        "--no-fund",
        "--offline",
        "--omit=optional",
        launcherTarball,
        runtimeTarball,
      ],
      targetPlatform: "linux-x64",
      version: BOXTEAM_VERSION,
      prefix: null,
      packageTarballs: [
        launcherTarball,
        runtimeTarball,
      ],
      buildScript: path.join(fixtureProjectRoot, "scripts", "release", "package-linux-x64.mjs"),
      buildResult: path.join(fixtureProjectRoot, "out", "packaging", "linux-x64", "build-result.json"),
    });
  });

  test("Windows 从本地 tarball 安装并支持隔离 prefix", () => {
    const command = buildReleaseInstallCommand({
      platform: "win32",
      architecture: "x64",
      projectRoot: fixtureProjectRoot,
      prefix: fixtureInstallPrefix,
    });

    expect(command.command).toBe("npm.cmd");
    expect(command.args).toEqual([
      "install",
      "--global",
      "--no-audit",
      "--no-fund",
      "--offline",
      "--omit=optional",
      "--prefix",
      fixtureInstallPrefix,
      path.join(fixtureProjectRoot, "out", "packaging", "windows-x64", "tarballs", `boxteam-${BOXTEAM_VERSION}.tgz`),
      path.join(fixtureProjectRoot, "out", "packaging", "windows-x64", "release-assets", `boxteam-runtime-windows-x64-${BOXTEAM_VERSION}.tgz`),
    ]);
  });

  test("只允许 prefix 参数", () => {
    expect(() => parseInstallArguments(["--unknown"])).toThrow(
      "未知发布安装参数",
    );
    expect(() => parseInstallArguments(["--prefix"])).toThrow(
      "--prefix 必须提供非空值",
    );
  });

  test("保留 npm 的底层失败状态", () => {
    const calls = [];
    expect(() =>
      runReleaseInstall({
        command: "npm",
        args: ["install"],
        spawnSyncImpl(command, args, options) {
          calls.push({ command, args, options });
          return { status: 17 };
        },
      }),
    ).toThrow("exit=17");
    expect(calls).toHaveLength(1);
    expect(calls[0].options.stdio).toBe("inherit");
  });
});
