import { describe, expect, test } from "bun:test";
import path from "node:path";

import {
  defaultBoxteamHome,
  initializeUserConfiguration,
} from "./config-bootstrap.mjs";

const fixtureRoot = path.resolve(
  "out/tests/packages/launcher/src/config-bootstrap/workspace/配置 用户目录",
);
const fixtureUserHome = path.join(fixtureRoot, "home");
const fixtureBoxteamHome = path.join(fixtureRoot, "boxteams");
const fixtureApplicationRoot = path.join(fixtureRoot, "application");
const fixturePythonExecutable = path.join(fixtureRoot, "runtime", "python");
const fixtureSourceRoot = path.join(fixtureRoot, "source");
const fixtureSourcePython = path.join(fixtureSourceRoot, ".venv", "bin", "python");

describe("config bootstrap", () => {
  test("开发与正式发行使用隔离 home", () => {
    expect(defaultBoxteamHome("source-development", fixtureUserHome)).toBe(
      path.join(fixtureUserHome, ".boxteams-dev"),
    );
    expect(defaultBoxteamHome("npm", fixtureUserHome)).toBe(
      path.join(fixtureUserHome, ".boxteams"),
    );
  });

  test("使用 manifest Python 执行缺失初始化", () => {
    const calls = [];
    const runtime = {
      distribution: "npm",
      pythonExecutable: fixturePythonExecutable,
      applicationRoot: fixtureApplicationRoot,
    };
    const result = initializeUserConfiguration({
      runtime,
      environment: {
        BOXTEAM_HOME: fixtureBoxteamHome,
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

    expect(calls[0].command).toBe(fixturePythonExecutable);
    expect(calls[0].args).toEqual(["-m", "configs.boxteam"]);
    expect(calls[0].options.cwd).toBe(fixtureApplicationRoot);
    expect(calls[0].options.env.BOXTEAM_HOME).toBe(fixtureBoxteamHome);
    expect(calls[0].options.env.BOXTEAM_INSTALL_DEVELOPMENT_ASSETS).toBe("1");
    expect(calls[0].options.env.BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE).toBe("1");
    expect(result.output).toContain('"created":true');
  });

  test("源码开发与安装发行使用相同初始化命令", () => {
    const calls = [];
    initializeUserConfiguration({
      runtime: {
        distribution: "source-development",
        pythonExecutable: fixtureSourcePython,
        applicationRoot: fixtureSourceRoot,
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
    expect(calls[0].args).toEqual(["-m", "configs.boxteam"]);
  });

  test("内置 Python 初始化失败不会静默继续", () => {
    expect(() =>
      initializeUserConfiguration({
        runtime: {
          distribution: "npm",
          pythonExecutable: fixturePythonExecutable,
          applicationRoot: fixtureApplicationRoot,
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

  for (const code of ["ENOENT", "EACCES", "ETIMEDOUT"]) {
    test(`内置 Python 启动失败保留${code}原始原因`, () => {
      const failure = Object.assign(new Error(`spawn ${code}`), {
        code, path: fixturePythonExecutable, syscall: "spawnSync",
      });
      let observed;
      try {
        initializeUserConfiguration({
          runtime: {
            distribution: "npm",
            pythonExecutable: fixturePythonExecutable,
            applicationRoot: fixtureApplicationRoot,
          },
          environment: { BOXTEAM_HOME: fixtureBoxteamHome },
          spawnSyncImpl() {
            return { error: failure, status: null, stdout: "", stderr: "" };
          },
        });
      } catch (error) {
        observed = error;
      }
      expect(observed).toBeInstanceOf(Error);
      expect(observed.message).toBe(
        `启动内置 Python 配置初始化失败: ${fixturePythonExecutable}: ${failure.message}`,
      );
      expect(observed.cause).toBe(failure);
    });
  }

  test("显式 force 参数传给配置生成器", () => {
    const calls = [];
    initializeUserConfiguration({
      runtime: {
        distribution: "npm",
        pythonExecutable: fixturePythonExecutable,
        applicationRoot: fixtureApplicationRoot,
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
