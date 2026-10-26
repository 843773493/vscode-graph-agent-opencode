import { createRuntimeLogger } from "../../../../src/shared/serverRuntimeLogging.mjs";
import { describe, expect, spyOn, test } from "bun:test";
import { EventEmitter } from "node:events";
import { createServer } from "node:http";

import { superviseGateway, waitForGateway } from "./supervisor.mjs";

async function within(promise, milliseconds = 300) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((resolve) => {
        timer = setTimeout(() => resolve({ stillWaiting: true }), milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function withHttpProbe(respond, exercise) {
  const observed = { requests: 0, closedConnections: 0 };
  const pending = [];
  const server = createServer((request, response) => {
    observed.requests += 1;
    request.socket.once("close", () => { observed.closedConnections += 1; });
    respond(request, response, observed);
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const probe = (options = {}) => {
      const result = waitForGateway({
        url: `http://127.0.0.1:${server.address().port}/health`,
        timeoutMs: 80, ...options,
      }).then(() => ({ ready: true }), (error) => ({ ready: false, error }));
      pending.push(result);
      return result;
    };
    await exercise(probe, observed);
  } finally {
    server.closeAllConnections();
    if (server.listening) {
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
    await Promise.all(pending);
  }
}

describe("Launcher Gateway就绪等待", () => {
  test("空URL在发送请求前拒绝", async () => {
    let requests = 0;
    await expect(waitForGateway({ url: "", fetchImpl: async () => { requests += 1; } }))
      .rejects.toThrow("URL 必须是非空字符串");
    expect(requests).toBe(0);
  });

  test("挂起请求在总预算结束时取消并释放连接", async () => {
    await withHttpProbe(() => {}, async (probe, observed) => {
      const result = await within(probe());
      expect(result.stillWaiting).toBeUndefined();
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("在 80ms 内未就绪");
      await Bun.sleep(20);
      expect(observed.requests).toBe(1);
      expect(observed.closedConnections).toBe(1);
    });
  });

  test("轮询间隔受剩余预算限制并保留HTTP失败原因", async () => {
    await withHttpProbe((_request, response) => {
      response.statusCode = 503;
      response.end();
    }, async (probe, observed) => {
      const result = await within(probe({ intervalMs: 500 }));
      expect(result.stillWaiting).toBeUndefined();
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("HTTP 503");
      expect(result.error.cause.message).toContain("HTTP 503");
      expect(observed.requests).toBe(1);
    });
  });

  test("成功响应只观察状态并释放持续输出的正文", async () => {
    await withHttpProbe((_request, response) => {
      response.writeHead(200);
      response.write("unneeded streaming body");
    }, async (probe, observed) => {
      expect(await probe()).toEqual({ ready: true });
      await Bun.sleep(20);
      expect(observed.closedConnections).toBe(1);
    });
  });

  test("系统校时不提前结束重试预算", async () => {
    let clock;
    const wallNow = Date.now.bind(Date);
    try {
      await withHttpProbe((_request, response, observed) => {
        if (observed.requests === 1) {
          clock = spyOn(Date, "now").mockImplementation(() => wallNow() + 60_000);
        }
        response.statusCode = observed.requests === 1 ? 503 : 200;
        response.end();
      }, async (probe, observed) => {
        expect(await probe({ timeoutMs: 800, intervalMs: 20 })).toEqual({ ready: true });
        expect(observed.requests).toBe(2);
      });
    } finally {
      clock?.mockRestore();
    }
  });

  test("超过预算才返回的200不能报告就绪", async () => {
    const result = await waitForGateway({
      url: "http://fixture.invalid/health", timeoutMs: 30,
      fetchImpl: async () => {
        await Bun.sleep(60);
        return { ok: true, status: 200, statusText: "OK" };
      },
    }).then(() => ({ ready: true }), (error) => ({ ready: false, error }));
    expect(result.ready).toBe(false);
    expect(result.error.message).toContain("在 30ms 内未就绪");
  });

  test("调用方预先取消时不发送请求并保留原原因", async () => {
    const controller = new AbortController();
    const reason = new Error("启动已取消");
    controller.abort(reason);
    let requests = 0;
    const error = await waitForGateway({
      url: "http://fixture.invalid/health", signal: controller.signal,
      fetchImpl: async () => { requests += 1; return { ok: true }; },
    }).then(() => null, (error) => error);
    expect(error).toBe(reason);
    expect(requests).toBe(0);
  });

  test("请求期间取消立即释放连接且不启动下一次重试", async () => {
    const controller = new AbortController();
    const reason = new Error("Gateway进程已退出");
    await withHttpProbe(() => controller.abort(reason), async (probe, observed) => {
      const result = await within(probe({ signal: controller.signal }));
      expect(result.stillWaiting).toBeUndefined();
      expect(result.error).toBe(reason);
      await Bun.sleep(20);
      expect(observed.requests).toBe(1);
      expect(observed.closedConnections).toBe(1);
    });
  });

  test("轮询睡眠期间取消不必等待完整间隔", async () => {
    const controller = new AbortController();
    const reason = new Error("停止启动观察");
    let cancelTimer;
    try {
      await withHttpProbe((_request, response) => {
        response.statusCode = 503;
        response.end();
        cancelTimer = setTimeout(() => controller.abort(reason), 20);
      }, async (probe, observed) => {
        const result = await within(probe({ signal: controller.signal, timeoutMs: 150, intervalMs: 500 }));
        expect(result.stillWaiting).toBeUndefined();
        expect(result.error).toBe(reason);
        expect(observed.requests).toBe(1);
      });
    } finally {
      clearTimeout(cancelTimer);
    }
  });

  test("真实supervisor入口在子进程退出后取消其未完成探针", async () => {
    const child = new EventEmitter();
    Object.assign(child, { exitCode: null, signalCode: null, kill() {} });
    let observedSignal;
    const result = superviseGateway({
      runtime: {
        distribution: "npm", version: "fixture", manifestPath: "fixture-manifest",
        applicationRoot: process.cwd(), pythonExecutable: "fixture-python",
        nodeExecutable: process.execPath, webAssets: null, chromiumExecutable: null,
      },
      environment: {}, openBrowser: false, spawnImpl: () => child,
      fetchImpl: (_url, options) => {
        observedSignal = options?.signal;
        queueMicrotask(() => {
          child.exitCode = 3;
          child.emit("exit", 3, null);
          child.emit("close", 3, null);
        });
        return new Promise((_resolve, reject) => {
          observedSignal?.addEventListener("abort", () => reject(observedSignal.reason), { once: true });
        });
      },
      logger: createRuntimeLogger("launcher", "test", { write() {} }), processObject: new EventEmitter(),
    });
    await expect(result).rejects.toThrow("Gateway 就绪前退出: exit=3");
    expect(observedSignal).toBeDefined();
    expect(observedSignal.aborted).toBe(true);
  });
});
