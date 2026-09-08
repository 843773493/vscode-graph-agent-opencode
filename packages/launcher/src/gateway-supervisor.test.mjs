import { describe, expect, test } from "bun:test";
import { EventEmitter } from "node:events";
import { createServer as createHttpServer } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import {
  forwardGatewayOutput,
  buildGatewayPendingEnvironment,
  createGatewayPublicListener,
  createGatewaySupervisorControl,
  gatewayEndpoint,
  gatewayEnvironment,
  gatewayStartupContract,
  installSignalForwarding,
  requestGatewayHandoff,
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
    for (const distribution of ["source-installed", "npm", "standalone"]) {
      expect(gatewayEndpoint(distribution)).toEqual({
        host: "127.0.0.1",
        port: 8114,
        url: "http://127.0.0.1:8114",
      });
    }
  });

  test("开发版允许通过环境变量切换 Gateway 端口", () => {
    expect(
      gatewayEndpoint("source-development", {
        BOXTEAM_GATEWAY_PORT: "8114",
      }),
    ).toEqual({
      host: "127.0.0.1",
      port: 8114,
      url: "http://127.0.0.1:8114",
    });
    expect(
      gatewayEnvironment(runtime, { BOXTEAM_GATEWAY_PORT: "8114" })
        .BOXTEAM_GATEWAY_URL,
    ).toBe("http://127.0.0.1:8114");
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
    expect(() => gatewayStartupContract({
      BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
    })).toThrow("Gateway generation");
    expect(() => gatewayStartupContract({
      BOXTEAM_CONFIG_GENERATION: "generation-2",
    })).toThrow("不完整");
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
    expect(calls[0].args.slice(-6)).toEqual([
      "--host",
      "127.0.0.1",
      "--port",
      "8114",
      "--timeout-graceful-shutdown",
      "2",
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
    expect(children[1].environment.BOXTEAM_CONFIG_CANDIDATE_REF).toBeUndefined();
    expect(children[1].environment.BOXTEAM_CONFIG_GENERATION).toBeUndefined();
    expect(children[1].environment.BOXTEAM_CONFIG_FENCING_TOKEN).toBeUndefined();
    expect(output.join("")).toContain("回退 active snapshot");
  });

  test("稳定 public listener 代理 HTTP 流并可切换 target", async () => {
    const upstream = createHttpServer((request, response) => {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(`method=${request.method}\n`);
      request.on("data", (chunk) => response.write(chunk));
      request.on("end", () => response.end("done\n"));
    });
    await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
    const upstreamAddress = upstream.address();
    const listener = createGatewayPublicListener({
      host: "127.0.0.1",
      port: 0,
    });
    await listener.listen();
    const publicAddress = listener.address();
    expect(typeof publicAddress).toBe("object");
    expect(typeof upstreamAddress).toBe("object");
    listener.setTarget({
      host: "127.0.0.1",
      port: upstreamAddress.port,
    });

    try {
      const response = await fetch(
        `http://127.0.0.1:${publicAddress.port}/events`,
        {
          method: "POST",
          body: "payload",
        },
      );
      expect(response.status).toBe(200);
      expect(await response.text()).toBe("method=POST\npayloaddone\n");
    } finally {
      await listener.close();
      await new Promise((resolve) => upstream.close(resolve));
    }
  });

  test("Gateway supervisor control socket 返回 handoff 结果", async () => {
    const boxteamHome = mkdtempSync(path.join(tmpdir(), "boxteam-launcher-"));
    const calls = [];
    const control = createGatewaySupervisorControl({
      socketPath: path.join(boxteamHome, "state", "gateway-supervisor.sock"),
      onHandoff: async (payload) => {
        calls.push(payload);
        return { accepted: true, target_generation: payload.target_generation };
      },
    });
    await control.listen();
    try {
      const result = await requestGatewayHandoff({
        boxteamHome,
        environment: {
          BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
          BOXTEAM_CONFIG_GENERATION: "generation-2",
          BOXTEAM_CONFIG_FENCING_TOKEN: "fence-2",
        },
      });
      expect(result).toEqual({
        handled: true,
        data: { accepted: true, target_generation: "generation-2" },
      });
      expect(calls).toEqual([
        {
          type: "gateway_pending_handoff",
          candidate_ref: "candidate-ref",
          target_generation: "generation-2",
          fencing_token: "fence-2",
        },
      ]);
    } finally {
      await control.close();
      rmSync(boxteamHome, { recursive: true, force: true });
    }
  });

  test("pending listener handoff 失败时保留旧 public generation", async () => {
    const boxteamHome = mkdtempSync(path.join(tmpdir(), "boxteam-launcher-"));
    const children = [];
    const processObject = new EventEmitter();
    const closeableChild = () => {
      const child = fakeChild();
      child.kill = (signal) => {
        child.killedWith.push(signal);
        if (child.exitCode !== null || child.signalCode !== null) return;
        child.exitCode = signal === "SIGKILL" ? 137 : 0;
        queueMicrotask(() => {
          child.emit("exit", child.exitCode, null);
          child.emit("close", child.exitCode, null);
        });
      };
      return child;
    };
    try {
      const resultPromise = superviseGateway({
        runtime,
        environment: {
          BOXTEAM_HOME: boxteamHome,
          BOXTEAM_GATEWAY_PORT: "38117",
        },
        openBrowser: false,
        spawnImpl(_command, args, options) {
          const child = closeableChild();
          const portArgumentIndex = args.indexOf("--port");
          const privatePort = Number(args[portArgumentIndex + 1]);
          const isPending = options.env.BOXTEAM_CONFIG_CANDIDATE_REF !== undefined;
          const server = createHttpServer((_request, response) => {
            response.end(isPending ? "pending" : "active");
          });
          void server.listen(privatePort, "127.0.0.1");
          const closeChild = child.kill;
          child.kill = (signal) => {
            closeChild(signal);
            void new Promise((resolve) => server.close(resolve));
          };
          children.push({ child, server });
          if (isPending) {
            setTimeout(() => {
              child.exitCode = 1;
              child.emit("exit", 1, null);
              child.emit("close", 1, null);
              void new Promise((resolve) => server.close(resolve));
            }, 20);
          }
          return child;
        },
        fetchImpl: async () =>
          children.length === 1
            ? { ok: true, status: 200, statusText: "OK" }
            : { ok: false, status: 503, statusText: "pending failed" },
        stdout: { write() {} },
        stderr: { write() {} },
        processObject,
      });

      await new Promise((resolve) => setTimeout(resolve, 20));
      const publicResponseBefore = await fetch(
        "http://127.0.0.1:38117/generation",
      );
      expect(await publicResponseBefore.text()).toBe("active");
      const handoff = await requestGatewayHandoff({
        boxteamHome,
        environment: {
          BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
          BOXTEAM_CONFIG_GENERATION: "generation-2",
          BOXTEAM_CONFIG_FENCING_TOKEN: "fence-2",
        },
      });
      expect(handoff.handled).toBe(true);
      expect(handoff.data.accepted).toBe(false);
      const publicResponseAfter = await fetch(
        "http://127.0.0.1:38117/generation",
      );
      expect(await publicResponseAfter.text()).toBe("active");

      processObject.emit("SIGTERM");
      expect(await resultPromise).toBe(0);
    } finally {
      for (const entry of children) {
        void new Promise((resolve) => entry.server.close(resolve));
      }
      rmSync(boxteamHome, { recursive: true, force: true });
    }
  });

  test("supervisor 在旧 generation 仍运行时完成 pending listener handoff", async () => {
    const boxteamHome = mkdtempSync(path.join(tmpdir(), "boxteam-launcher-"));
    const children = [];
    const processObject = new EventEmitter();
    const closeableChild = () => {
      const child = fakeChild();
      child.kill = (signal) => {
        child.killedWith.push(signal);
        if (child.exitCode !== null || child.signalCode !== null) return;
        child.exitCode = signal === "SIGKILL" ? 137 : 0;
        queueMicrotask(() => {
          child.emit("exit", child.exitCode, null);
          child.emit("close", child.exitCode, null);
        });
      };
      return child;
    };
    try {
      const resultPromise = superviseGateway({
        runtime,
        environment: {
          BOXTEAM_HOME: boxteamHome,
          BOXTEAM_GATEWAY_PORT: "38116",
        },
        openBrowser: false,
        spawnImpl(_command, args, options) {
          const child = closeableChild();
          const portArgumentIndex = args.indexOf("--port");
          const privatePort = Number(args[portArgumentIndex + 1]);
          const server = createHttpServer((_request, response) => {
            response.end(
              options.env.BOXTEAM_CONFIG_CANDIDATE_REF === undefined
                ? "active"
                : "pending",
            );
          });
          void server.listen(privatePort, "127.0.0.1");
          const closeChildServer = child.kill;
          child.kill = (signal) => {
            closeChildServer(signal);
            void new Promise((resolve) => server.close(resolve));
          };
          children.push({ child, args, environment: options.env, server });
          return child;
        },
        fetchImpl: async () => ({ ok: true, status: 200, statusText: "OK" }),
        stdout: { write() {} },
        stderr: { write() {} },
        processObject,
      });

      await new Promise((resolve) => setTimeout(resolve, 20));
      const publicResponseBefore = await fetch(
        "http://127.0.0.1:38116/generation",
      );
      expect(await publicResponseBefore.text()).toBe("active");
      const handoff = await requestGatewayHandoff({
        boxteamHome,
        environment: {
          BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
          BOXTEAM_CONFIG_GENERATION: "generation-2",
          BOXTEAM_CONFIG_FENCING_TOKEN: "fence-2",
        },
      });
      expect(handoff).toEqual({
        handled: true,
        data: { accepted: true, target_generation: "generation-2" },
      });
      expect(children).toHaveLength(2);
      expect(children[0].child.killedWith).toEqual(["SIGTERM"]);
      expect(children[1].environment.BOXTEAM_CONFIG_CANDIDATE_REF).toBe(
        "candidate-ref",
      );
      const portArgumentIndex = children[1].args.indexOf("--port");
      expect(portArgumentIndex).toBeGreaterThan(-1);
      expect(children[1].args[portArgumentIndex + 1]).not.toBe("38116");
      const publicResponseAfter = await fetch(
        "http://127.0.0.1:38116/generation",
      );
      expect(await publicResponseAfter.text()).toBe("pending");

      processObject.emit("SIGTERM");
      expect(await resultPromise).toBe(0);
    } finally {
      for (const entry of children) {
        void new Promise((resolve) => entry.server.close(resolve));
      }
      rmSync(boxteamHome, { recursive: true, force: true });
    }
  });
});
