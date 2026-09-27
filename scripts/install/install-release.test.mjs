import { describe, expect, test } from "bun:test";

import { BOXTEAM_VERSION } from "../../packaging/runtime/versions.mjs";
import {
  buildReleaseInstallCommand,
  parseInstallArguments,
  runReleaseInstall,
} from "./install-release.mjs";

describe("发布版安装入口", () => {
  test("默认从当前源码的 Linux 本地 tarball 安装", () => {
    const command = buildReleaseInstallCommand({
      platform: "linux",
      architecture: "x64",
      projectRoot: "/workspace",
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
        `/workspace/out/packaging/linux-x64/tarballs/boxteam-${BOXTEAM_VERSION}.tgz`,
        `/workspace/out/packaging/linux-x64/release-assets/boxteam-runtime-linux-x64-${BOXTEAM_VERSION}.tgz`,
      ],
      targetPlatform: "linux-x64",
      version: BOXTEAM_VERSION,
      prefix: null,
      packageTarballs: [
        `/workspace/out/packaging/linux-x64/tarballs/boxteam-${BOXTEAM_VERSION}.tgz`,
        `/workspace/out/packaging/linux-x64/release-assets/boxteam-runtime-linux-x64-${BOXTEAM_VERSION}.tgz`,
      ],
      buildScript: "/workspace/scripts/release/package-linux-x64.mjs",
      buildResult: "/workspace/out/packaging/linux-x64/build-result.json",
    });
  });

  test("Windows 从本地 tarball 安装并支持隔离 prefix", () => {
    const command = buildReleaseInstallCommand({
      platform: "win32",
      architecture: "x64",
      projectRoot: "/workspace",
      prefix: "/tmp/boxteam-install",
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
      "/tmp/boxteam-install",
      `/workspace/out/packaging/windows-x64/tarballs/boxteam-${BOXTEAM_VERSION}.tgz`,
      `/workspace/out/packaging/windows-x64/release-assets/boxteam-runtime-windows-x64-${BOXTEAM_VERSION}.tgz`,
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
