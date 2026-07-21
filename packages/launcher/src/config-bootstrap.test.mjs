import { describe, expect, test } from "bun:test";

import {
  defaultBoxteamHome,
  initializeUserConfiguration,
} from "./config-bootstrap.mjs";

describe("config bootstrap", () => {
  test("开发与正式发行使用隔离 home", () => {
    expect(defaultBoxteamHome("source-development", "/home/test")).toBe(
      "/home/test/.boxteams-dev",
    );
    expect(defaultBoxteamHome("npm", "/home/test")).toBe(
      "/home/test/.boxteams",
    );
  });

  test("使用 manifest Python 执行缺失初始化", () => {
    const calls = [];
    const runtime = {
      distribution: "npm",
      pythonExecutable: "/runtime/python/bin/python",
      applicationRoot: "/runtime/application",
    };
    const result = initializeUserConfiguration({
      runtime,
      environment: {
        BOXTEAM_HOME: "/tmp/boxteams",
        BOXTEAM_INSTALL_DEVELOPMENT_ASSETS: "1",
        BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE: "1",
      },
      spawnSyncImpl(command, args, options) {
        calls.push({ command, args, options });
        return {
          status: 0,
          stdout: '{"created":true}\n',
          stderr: "",
        };
      },
    });

    expect(calls[0].command).toBe("/runtime/python/bin/python");
    expect(calls[0].args).toEqual(["-m", "configs.boxteam"]);
    expect(calls[0].options.cwd).toBe("/runtime/application");
    expect(calls[0].options.env.BOXTEAM_HOME).toBe("/tmp/boxteams");
    expect(calls[0].options.env.BOXTEAM_INSTALL_DEVELOPMENT_ASSETS).toBe("0");
    expect(calls[0].options.env.BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE).toBe("0");
    expect(result.output).toContain('"created":true');
  });

  test("源码开发保留显式开发资产开关", () => {
    const calls = [];
    initializeUserConfiguration({
      runtime: {
        distribution: "source-development",
        pythonExecutable: "/repo/.venv/bin/python",
        applicationRoot: "/repo",
      },
      environment: {
        BOXTEAM_INSTALL_DEVELOPMENT_ASSETS: "1",
        BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE: "1",
      },
      spawnSyncImpl(command, args, options) {
        calls.push({ command, args, options });
        return { status: 0, stdout: "", stderr: "" };
      },
    });

    expect(calls[0].options.env.BOXTEAM_INSTALL_DEVELOPMENT_ASSETS).toBe("1");
    expect(calls[0].options.env.BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE).toBe("1");
    expect(calls[0].args).toEqual([
      "-m",
      "configs.boxteam",
      "--project-root",
      "/repo",
    ]);
  });

  test("内置 Python 初始化失败不会静默继续", () => {
    expect(() =>
      initializeUserConfiguration({
        runtime: {
          distribution: "npm",
          pythonExecutable: "/runtime/python",
          applicationRoot: "/runtime/application",
        },
        environment: {},
        spawnSyncImpl() {
          return {
            status: 2,
            stdout: "",
            stderr: "invalid config",
          };
        },
      }),
    ).toThrow("invalid config");
  });

  test("显式 force 参数传给配置生成器", () => {
    const calls = [];
    initializeUserConfiguration({
      runtime: {
        distribution: "npm",
        pythonExecutable: "/runtime/python",
        applicationRoot: "/runtime/application",
      },
      environment: {},
      args: ["--force"],
      spawnSyncImpl(command, args, options) {
        calls.push({ command, args, options });
        return { status: 0, stdout: "", stderr: "" };
      },
    });

    expect(calls[0].args).toEqual([
      "-m",
      "configs.boxteam",
      "--force",
    ]);
  });
});
