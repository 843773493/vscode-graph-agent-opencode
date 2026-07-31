import { describe, expect, test } from "bun:test";
import { EventEmitter } from "node:events";

import {
  forwardGatewayOutput,
  gatewayEndpoint,
  gatewayEnvironment,
  installSignalForwarding,
  spawnGateway,
  superviseGateway,
} from "./gateway-supervisor.mjs";

function fakeChild() {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.killedWith = [];
  child.kill = (signal) => {
    child.killedWith.push(signal);
  };
  return child;
}

const runtime = {
  distribution: "source-development",
  version: "0.1.0",
  manifestPath: "/runtime/runtime-manifest.json",
  pythonExecutable: "/runtime/python",
  applicationRoot: "/runtime/application",
  nodeExecutable: "/usr/bin/node",
  webAssets: null,
  chromiumExecutable: "/runtime/chromium",
};

describe("gateway supervisor", () => {
  test("开发版与安装版使用隔离的 Gateway 端口", () => {
    expect(gatewayEndpoint("source-development")).toEqual({
      host: "127.0.0.1",
      port: 8014,
      url: "http://127.0.0.1:8014",
    });
    for (const distribution of ["source-installed", "npm"]) {
      expect(gatewayEndpoint(distribution)).toEqual({
        host: "127.0.0.1",
        port: 8114,
        url: "http://127.0.0.1:8114",
      });
    }
  });

  test("向 Gateway 传入 manifest 资源", () => {
    const environment = gatewayEnvironment(runtime, {
      BOXTEAM_HOME: "/tmp/boxteams",
    });

    expect(environment.BOXTEAM_RUNTIME_MANIFEST).toBe(runtime.manifestPath);
    expect(environment.BOXTEAM_NODE_BIN).toBe("/usr/bin/node");
    expect(environment.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH).toBe(
      "/runtime/chromium",
    );
    expect(environment.BOXTEAM_GATEWAY_URL).toBe("http://127.0.0.1:8014");
  });

  test("安装版使用 8114 启动 Gateway", () => {
    const calls = [];
    const installedRuntime = { ...runtime, distribution: "npm" };
    spawnGateway({
      runtime: installedRuntime,
      environment: {},
      spawnImpl(command, args, options) {
        calls.push({ command, args, options });
        return fakeChild();
      },
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].args.slice(-4)).toEqual([
      "--host",
      "127.0.0.1",
      "--port",
      "8114",
    ]);
    expect(calls[0].options.env.BOXTEAM_GATEWAY_URL).toBe(
      "http://127.0.0.1:8114",
    );
    expect(calls[0].options.detached).toBe(process.platform !== "win32");
  });

  test("POSIX Gateway 使用独立进程组，Windows 保持普通子进程", () => {
    const detachedValues = [];
    for (const platform of ["linux", "darwin", "win32"]) {
      spawnGateway({
        runtime,
        environment: {},
        platform,
        spawnImpl(_command, _args, options) {
          detachedValues.push(options.detached);
          return fakeChild();
        },
      });
    }

    expect(detachedValues).toEqual([true, true, false]);
  });

  test("只转发一次关闭信号并清理监听器", () => {
    const child = fakeChild();
    const processObject = new EventEmitter();
    const remove = installSignalForwarding(child, processObject, "linux");

    processObject.emit("SIGINT");
    processObject.emit("SIGINT");
    processObject.emit("SIGTERM");
    expect(child.killedWith).toEqual(["SIGINT"]);
    remove();
    processObject.emit("SIGHUP");
    expect(child.killedWith).toEqual(["SIGINT"]);
  });

  test("Windows SIGBREAK 转换为 Gateway 可处理的 SIGTERM", () => {
    const child = fakeChild();
    const processObject = new EventEmitter();
    installSignalForwarding(child, processObject, "win32");

    processObject.emit("SIGBREAK");

    expect(child.killedWith).toEqual(["SIGTERM"]);
  });

  test("将 Gateway 输出转发到 Launcher 输出流", () => {
    const child = fakeChild();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const stdout = [];
    const stderr = [];
    const remove = forwardGatewayOutput(
      child,
      { write: (value) => stdout.push(String(value)) },
      { write: (value) => stderr.push(String(value)) },
    );

    child.stdout.emit("data", "gateway stdout\n");
    child.stderr.emit("data", "gateway stderr\n");
    remove();
    child.stdout.emit("data", "ignored\n");

    expect(stdout).toEqual(["gateway stdout\n"]);
    expect(stderr).toEqual(["gateway stderr\n"]);
  });

  test("Gateway 就绪后以前台退出码结束", async () => {
    const child = fakeChild();
    const processObject = new EventEmitter();
    const output = [];
    const resultPromise = superviseGateway({
      runtime,
      environment: {},
      openBrowser: false,
      spawnImpl() {
        setTimeout(() => {
          child.exitCode = 0;
          child.emit("exit", 0, null);
          child.emit("close", 0, null);
        }, 20);
        return child;
      },
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        statusText: "OK",
      }),
      stdout: {
        write(value) {
          output.push(value);
        },
      },
      stderr: {
        write() {},
      },
      processObject,
    });

    expect(await resultPromise).toBe(0);
    expect(output.join("")).toContain("Gateway 已就绪");
    expect(output.join("")).toContain("http://127.0.0.1:8014");
    expect(processObject.listenerCount("SIGTERM")).toBe(0);
  });
});
