import { describe, expect, test } from "bun:test";

import { BOXTEAM_VERSION } from "../../packaging/runtime/versions.mjs";
import {
  buildReleaseInstallCommand,
  parseInstallArguments,
  runReleaseInstall,
} from "./install-release.mjs";

describe("发布版安装入口", () => {
  test("默认使用唯一发布版本来源和平台 runtime", () => {
    const command = buildReleaseInstallCommand({ platform: "linux" });

    expect(command).toEqual({
      command: "npm",
      args: [
        "install",
        "--global",
        "--no-audit",
        "--no-fund",
        `boxteam@${BOXTEAM_VERSION}`,
      ],
      packageSpec: `boxteam@${BOXTEAM_VERSION}`,
    });
  });

  test("Windows 使用 npm.cmd 并支持隔离 prefix", () => {
    const command = buildReleaseInstallCommand({
      platform: "win32",
      prefix: "/tmp/boxteam-install",
    });

    expect(command.command).toBe("npm.cmd");
    expect(command.args).toEqual([
      "install",
      "--global",
      "--no-audit",
      "--no-fund",
      "--prefix",
      "/tmp/boxteam-install",
      `boxteam@${BOXTEAM_VERSION}`,
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
