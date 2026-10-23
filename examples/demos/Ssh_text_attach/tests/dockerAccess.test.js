import { describe, expect, test } from "bun:test";
import {
  describeCommandCheckResult,
  resolveDockerCommandPrefix,
} from "../scripts/dockerAccess.js";

describe("Docker CLI 平台选择", () => {
  test("所有平台优先直接调用 Docker CLI", () => {
    for (const platform of ["linux", "win32", "darwin"]) {
      const calls = [];
      const prefix = resolveDockerCommandPrefix({
        platform,
        checkCommand: (command, args) => {
          calls.push([command, args]);
          return { succeeded: true, details: "" };
        },
        sudoEnvironmentPairs: [],
      });

      expect(prefix).toEqual(["docker"]);
      expect(calls).toEqual([["docker", ["version"]]]);
    }
  });

  test("Linux 直接访问失败时用无交互 sudo 传递显式环境", () => {
    const calls = [];
    const sudoEnvironmentPairs = [
      "HTTPS_PROXY=http://proxy.example.test:8080",
      "SSH_TEXT_BASE_IMAGE=demo-node:22",
    ];

    const prefix = resolveDockerCommandPrefix({
      platform: "linux",
      checkCommand: (command, args) => {
        calls.push([command, args]);
        return { succeeded: command === "sudo", details: "退出码=1" };
      },
      sudoEnvironmentPairs,
    });

    expect(calls).toEqual([
      ["docker", ["version"]],
      ["sudo", ["-n", "env", ...sudoEnvironmentPairs, "docker", "version"]],
    ]);
    expect(prefix).toEqual(["sudo", "-n", "env", ...sudoEnvironmentPairs, "docker"]);
  });

  test("Windows 和 macOS 保留 Docker CLI 启动错误且不尝试 sudo", () => {
    for (const platform of ["win32", "darwin"]) {
      const calls = [];
      expect(() => resolveDockerCommandPrefix({
        platform,
        checkCommand: (command, args) => {
          calls.push([command, args]);
          return {
            succeeded: false,
            details: "启动错误=ENOENT: spawnSync docker ENOENT；退出码=null；信号=null",
          };
        },
        sudoEnvironmentPairs: [],
      })).toThrow("docker: 启动错误=ENOENT: spawnSync docker ENOENT");
      expect(calls).toEqual([["docker", ["version"]]]);
    }
  });

  test("Linux 错误会同时说明 Docker CLI 和 sudo 检查结果", () => {
    const calls = [];
    expect(() => resolveDockerCommandPrefix({
      platform: "linux",
      checkCommand: (command, args) => {
        calls.push([command, args]);
        return command === "docker"
          ? {
              succeeded: false,
              details: "启动错误=ENOENT: spawnSync docker ENOENT；退出码=null；信号=null",
            }
          : {
              succeeded: false,
              details: "退出码=1；信号=null；stderr=sudo: a password is required",
            };
      },
      sudoEnvironmentPairs: ["SSH_TEXT_BASE_IMAGE=demo-node:22"],
    })).toThrow(
      "无法访问 Docker daemon：docker: 启动错误=ENOENT: spawnSync docker ENOENT；退出码=null；信号=null；sudo: 退出码=1；信号=null；stderr=sudo: a password is required",
    );
    expect(calls).toEqual([
      ["docker", ["version"]],
      ["sudo", ["-n", "env", "SSH_TEXT_BASE_IMAGE=demo-node:22", "docker", "version"]],
    ]);
  });

  test("子进程诊断保留启动错误、退出码、信号和标准错误", () => {
    const error = Object.assign(new Error("spawnSync docker ENOENT"), { code: "ENOENT" });
    expect(describeCommandCheckResult({
      error,
      status: null,
      signal: null,
      stderr: "",
    })).toEqual({
      succeeded: false,
      details: "启动错误=ENOENT: spawnSync docker ENOENT；退出码=null；信号=null",
    });
    expect(describeCommandCheckResult({
      error: undefined,
      status: null,
      signal: "SIGTERM",
      stderr: "",
    })).toEqual({
      succeeded: false,
      details: "退出码=null；信号=SIGTERM",
    });
    expect(describeCommandCheckResult({
      error: undefined,
      status: 1,
      signal: null,
      stderr: "daemon is unavailable\n",
    })).toEqual({
      succeeded: false,
      details: "退出码=1；信号=null；stderr=daemon is unavailable",
    });
  });
});
