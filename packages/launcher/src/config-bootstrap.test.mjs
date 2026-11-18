import { describe, expect, test } from "bun:test";
import path from "node:path";

import {
  defaultBoxteamHome,
  initializeUserConfiguration,
  stopLauncherForSupportedUpgrade,
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
  for (const required of [false, true]) {
    test(`升级预检${required ? "停旧实例" : "保留同版本实例"}`, async () => {
      const stopped = [];
      const runtime = { distribution: "npm", pythonExecutable: fixturePythonExecutable, node: {source: "launcher", executable: null}, bundledPlugins: [],
        applicationRoot: fixtureApplicationRoot };
      const result = await stopLauncherForSupportedUpgrade({
        runtime, environment: { BOXTEAM_HOME: fixtureBoxteamHome },
        targetLock: { value: { token: "original-instance" } },
        spawnSyncImpl(_command, args) {
          expect(args).toEqual(["-m", "configs.boxteam", "initialize", "--preview", "--bundled-plugins", "[]"]);
          return { status: 0, stdout: JSON.stringify({ upgrade_required: required }), stderr: "" };
        },
        async stopLauncherImpl({ boxteamHome, expectedToken }) {
          stopped.push({ boxteamHome, expectedToken });
          return { status: "stopped", pid: 123 };
        },
      });
      expect(stopped).toEqual(required ? [{ boxteamHome: fixtureBoxteamHome, expectedToken: "original-instance" }] : []);
      expect(result).toEqual(required ? { status: "stopped", pid: 123 } : null);
    });
  }
  test("未知版本预检失败不停止原服务", async () => {
    let stopped = false;
    await expect(stopLauncherForSupportedUpgrade({
      runtime: { distribution: "npm", pythonExecutable: fixturePythonExecutable, node: {source: "launcher", executable: null}, bundledPlugins: [],
        applicationRoot: fixtureApplicationRoot }, environment: { BOXTEAM_HOME: fixtureBoxteamHome },
      targetLock: { value: { token: "original-instance" } },
      spawnSyncImpl() { return { status: 1, stderr: "unknown version" }; },
      async stopLauncherImpl() { stopped = true; },
    })).rejects.toThrow("unknown version");
    expect(stopped).toBe(false);
  });
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
      pythonExecutable: fixturePythonExecutable, node: {source: "bundled", executable: "/target/node"},
      bundledPlugins: [{package_id: "boxteam.developer-tools", package_version: "0.1.0", archive: "/target/developer-tools.zip"}],
      applicationRoot: fixtureApplicationRoot,
    };
    const result = initializeUserConfiguration({
      runtime,
      environment: {
        BOXTEAM_HOME: fixtureBoxteamHome,
        PYTHONIOENCODING: "ascii",
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
    expect(calls[0].args).toEqual(["-m", "configs.boxteam", "--bundled-plugins", JSON.stringify(runtime.bundledPlugins)]);
    expect(calls[0].options.env.BOXTEAM_NODE_BIN).toBe("/target/node");
    expect(calls[0].options.cwd).toBe(fixtureApplicationRoot);
    expect(calls[0].options.env.BOXTEAM_HOME).toBe(fixtureBoxteamHome);
    expect(calls[0].options.env.PYTHONIOENCODING).toBe("utf-8");
    expect(calls[0].options.env.BOXTEAM_INSTALL_DEVELOPMENT_ASSETS).toBe("1");
    expect(calls[0].options.env.BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE).toBe("1");
    expect(result.output).toContain('"created":true');
  });

  test("源码开发与安装发行使用相同初始化命令", () => {
    const calls = [];
    initializeUserConfiguration({
      runtime: {
        distribution: "source-development",
        pythonExecutable: fixtureSourcePython, node: {source: "launcher", executable: null}, bundledPlugins: [],
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
    expect(calls[0].args).toEqual(["-m", "configs.boxteam", "--bundled-plugins", "[]"]);
  });

  test("内置 Python 初始化失败不会静默继续", () => {
    expect(() =>
      initializeUserConfiguration({
        runtime: {
          distribution: "npm",
          pythonExecutable: fixturePythonExecutable, node: {source: "launcher", executable: null}, bundledPlugins: [],
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
            pythonExecutable: fixturePythonExecutable, node: {source: "launcher", executable: null}, bundledPlugins: [],
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
        pythonExecutable: fixturePythonExecutable, node: {source: "launcher", executable: null}, bundledPlugins: [],
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
      "--force", "--bundled-plugins", "[]",
    ]);
  });
});
