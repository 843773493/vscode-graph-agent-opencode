import { describe, expect, test } from "bun:test";
import { EventEmitter } from "node:events";

import {
  forwardGatewayOutput,
  buildGatewayPendingEnvironment,
  gatewayEndpoint,
  gatewayEnvironment,
  gatewayStartupContract,
  installSignalForwarding,
  spawnGateway,
  superviseGateway,
} from "./supervisor.mjs";

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
  version: "test-version",
  manifestPath: "/runtime/runtime-manifest.json",
  pythonExecutable: "/runtime/python",
  applicationRoot: "/runtime/application",
  nodeExecutable: "/usr/bin/node",
  webAssets: null,
  chromiumExecutable: "/runtime/chromium",
};

describe("gateway supervisor", () => {
  test("源码开发默认端口与安装版共用 Gateway 端口", () => {
    expect(gatewayEndpoint("source-development")).toEqual({
      host: "127.0.0.1",
      port: 8014,
      url: "http://127.0.0.1:8014",
    });
    for (const distribution of ["source-installed", "npm", "standalone"]) {
      expect(gatewayEndpoint(distribution)).toEqual({
        host: "127.0.0.1",
        port: 8014,
        url: "http://127.0.0.1:8014",
      });
    }
  });

  test("开发版允许通过环境变量切换 Gateway 端口", () => {
    expect(
      gatewayEndpoint("source-development", {
        BOXTEAM_GATEWAY_PORT: "38114",
      }),
    ).toEqual({
      host: "127.0.0.1",
      port: 38114,
      url: "http://127.0.0.1:38114",
    });
    expect(
      gatewayEnvironment(runtime, { BOXTEAM_GATEWAY_PORT: "38114" })
        .BOXTEAM_GATEWAY_URL,
    ).toBe("http://127.0.0.1:38114");
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

  test("普通恢复只加载 active，pending 启动只传递不透明三元组", () => {
    const active = gatewayStartupContract({});
    expect(active.loadedSource).toBe("active");
    expect(active.candidateRef).toBeNull();

    const pendingEnvironment = buildGatewayPendingEnvironment(
      { BOXTEAM_HOME: "/tmp/boxteams" },
      {
        candidate_ref: "candidate-ref",
        target_generation: "generation-2",
        fencing_token: "fence-2",
      },
    );
    const pending = gatewayStartupContract(pendingEnvironment);
    expect(pending.loadedSource).toBe("pending");
    expect(pending.candidateRef).toBe("candidate-ref");
    expect(pending.generation).toBe("generation-2");
    expect(pending.fencingToken).toBe("fence-2");
    expect(pendingEnvironment.BOXTEAM_CONFIG_CANDIDATE_REF).toBe(
      "candidate-ref",
    );
    expect(pendingEnvironment.BOXTEAM_CONFIG_PAYLOAD).toBeUndefined();
  });

  test("拒绝不完整的 Gateway pending 启动契约", () => {
    expect(() =>
      gatewayStartupContract({
        BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
      }),
    ).toThrow("Gateway generation");
    expect(() =>
      gatewayStartupContract({
        BOXTEAM_CONFIG_GENERATION: "generation-2",
      }),
    ).toThrow("不完整");
  });

  test("安装版使用 8014 启动 Gateway", () => {
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
    expect(calls[0].args.slice(-6)).toEqual([
      "--host",
      "127.0.0.1",
      "--port",
      "8014",
      "--timeout-graceful-shutdown",
      "2",
    ]);
    expect(calls[0].options.env.BOXTEAM_GATEWAY_URL).toBe(
      "http://127.0.0.1:8014",
    );
    expect(calls[0].options.env.BOXTEAM_DEFAULT_BACKEND_PORT).toBe("8010");
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

  test("Gateway 优雅关闭超时后强制退出", () => {
    const child = fakeChild();
    const processObject = new EventEmitter();
    const callbacks = [];
    const errors = [];
    const remove = installSignalForwarding(child, processObject, "linux", {
      shutdownTimeoutMs: 10,
      setTimeoutImpl(callback) {
        callbacks.push(callback);
        return { unref() {} };
      },
      clearTimeoutImpl() {},
      stderr: { write: (value) => errors.push(String(value)) },
    });

    processObject.emit("SIGTERM");
    callbacks[0]();
    remove();

    expect(child.killedWith).toEqual(["SIGTERM", "SIGKILL"]);
    expect(errors.join("")).toContain("Gateway 未在 10ms 内退出");
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
      environment: { BOXTEAM_GATEWAY_PORT: "38114" },
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
    expect(output.join("")).toContain("http://127.0.0.1:38114");
    expect(processObject.listenerCount("SIGTERM")).toBe(0);
  });

  test("发布版 Gateway 直接监听 8014，不创建 Launcher 反向代理", async () => {
    const child = fakeChild();
    const processObject = new EventEmitter();
    const calls = [];
    const healthUrls = [];
    const installedRuntime = { ...runtime, distribution: "npm" };
    const resultPromise = superviseGateway({
      runtime: installedRuntime,
      environment: {},
      openBrowser: false,
      spawnImpl(_command, args, options) {
        calls.push({ args, options });
        setTimeout(() => {
          child.exitCode = 0;
          child.emit("exit", 0, null);
          child.emit("close", 0, null);
        }, 20);
        return child;
      },
      fetchImpl: async (url) => {
        healthUrls.push(url);
        return { ok: true, status: 200, statusText: "OK" };
      },
      stdout: { write() {} },
      stderr: { write() {} },
      processObject,
    });

    expect(await resultPromise).toBe(0);
    expect(calls).toHaveLength(1);
    const portArgumentIndex = calls[0].args.indexOf("--port");
    expect(calls[0].args[portArgumentIndex + 1]).toBe("8014");
    expect(healthUrls).toEqual(["http://127.0.0.1:8014/api/gateway/health"]);
    expect(processObject.listenerCount("SIGTERM")).toBe(0);
  });

  test("pending Gateway 未就绪退出时回退到 active snapshot", async () => {
    const children = [];
    const output = [];
    const pendingEnvironment = {
      BOXTEAM_GATEWAY_PORT: "38115",
      BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
      BOXTEAM_CONFIG_GENERATION: "generation-2",
      BOXTEAM_CONFIG_FENCING_TOKEN: "fence-2",
    };
    const resultPromise = superviseGateway({
      runtime,
      environment: pendingEnvironment,
      openBrowser: false,
      spawnImpl(_command, _args, options) {
        const child = fakeChild();
        children.push({ child, environment: options.env });
        if (children.length === 1) {
          setTimeout(() => {
            child.exitCode = 1;
            child.emit("exit", 1, null);
            child.emit("close", 1, null);
          }, 10);
        } else {
          setTimeout(() => {
            child.exitCode = 0;
            child.emit("exit", 0, null);
            child.emit("close", 0, null);
          }, 20);
        }
        return child;
      },
      fetchImpl: async () =>
        children.length === 1
          ? { ok: false, status: 503, statusText: "pending failed" }
          : { ok: true, status: 200, statusText: "OK" },
      stdout: { write: (value) => output.push(String(value)) },
      stderr: { write() {} },
      processObject: new EventEmitter(),
    });

    expect(await resultPromise).toBe(0);
    expect(children).toHaveLength(2);
    expect(children[0].environment.BOXTEAM_CONFIG_CANDIDATE_REF).toBe(
      "candidate-ref",
    );
    expect(
      children[1].environment.BOXTEAM_CONFIG_CANDIDATE_REF,
    ).toBeUndefined();
    expect(children[1].environment.BOXTEAM_CONFIG_GENERATION).toBeUndefined();
    expect(
      children[1].environment.BOXTEAM_CONFIG_FENCING_TOKEN,
    ).toBeUndefined();
    expect(output.join("")).toContain("回退 active snapshot");
  });

});
