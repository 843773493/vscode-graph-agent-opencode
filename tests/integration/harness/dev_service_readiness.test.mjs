import { afterAll, describe, expect, spyOn, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";

import { waitForDetachedReady, waitForHttpOk } from "../../../scripts/launch/dev-readiness.mjs";

const runtimeRoot = path.resolve(
  "out/tests/integration/harness/dev_service_readiness/runtime", randomUUID(),
);
afterAll(() => rmSync(runtimeRoot, { recursive: true, force: true }));

async function within(promise, milliseconds = 600) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((resolve) => { timer = setTimeout(() => resolve({ stillWaiting: true }), milliseconds); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function withHttpProbe(respond, exercise) {
  const observed = { requests: 0, closedConnections: 0, headers: [] };
  const pending = [];
  const server = createServer((request, response) => {
    observed.requests += 1;
    observed.headers.push(request.headers);
    request.socket.once("close", () => { observed.closedConnections += 1; });
    respond(request, response, observed);
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const probe = (options = {}) => {
      const result = waitForHttpOk({
        url: `http://127.0.0.1:${server.address().port}/health`,
        label: "isolated service", timeoutMs: 100, ...options,
      }).then(() => ({ ready: true }), (error) => ({ ready: false, error }));
      pending.push(result);
      return result;
    };
    await exercise(probe, observed);
  } finally {
    server.closeAllConnections();
    if (server.listening) await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    await Promise.all(pending);
  }
}

describe("开发栈各服务HTTP就绪", () => {
  test("挂起请求在总预算内取消并关闭响应连接", async () => {
    await withHttpProbe(() => {}, async (probe, observed) => {
      const result = await within(probe());
      expect(result.stillWaiting).toBeUndefined();
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("在 100ms 内未就绪");
      await Bun.sleep(20);
      expect(observed.requests).toBe(1);
      expect(observed.closedConnections).toBe(1);
    });
  });

  test("超过预算才返回的200不能报告就绪", async () => {
    await withHttpProbe((request, response) => {
      const timer = setTimeout(() => response.end("late"), 250);
      request.socket.once("close", () => clearTimeout(timer));
    }, async (probe, observed) => {
      const result = await within(probe());
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("在 100ms 内未就绪");
      expect(observed.requests).toBe(1);
    });
  });

  test("轮询间隔不越过剩余预算并保留最后HTTP状态", async () => {
    await withHttpProbe((_request, response) => {
      response.statusCode = 503;
      response.end();
    }, async (probe, observed) => {
      const started = performance.now();
      const result = await probe();
      expect(performance.now() - started).toBeLessThan(220);
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("HTTP 503");
      expect(observed.requests).toBe(1);
    });
  });

  test("预算内重试保留显式认证与工作区请求头", async () => {
    await withHttpProbe((_request, response, observed) => {
      response.statusCode = observed.requests === 1 ? 503 : 200;
      response.end();
    }, async (probe, observed) => {
      expect(await probe({ timeoutMs: 1000, headers: {
        "X-Local-Token": "fixture-token", "X-BoxTeam-Workspace-Id": "fixture-workspace",
      } })).toEqual({ ready: true });
      expect(observed.requests).toBe(2);
      for (const headers of observed.headers) {
        expect(headers["x-local-token"]).toBe("fixture-token");
        expect(headers["x-boxteam-workspace-id"]).toBe("fixture-workspace");
      }
    });
  });

  test("健康请求期间进程退出也必须拒绝就绪", async () => {
    const processHandle = { exitCode: null, signalCode: null };
    await withHttpProbe((_request, response) => {
      processHandle.exitCode = 7;
      response.end();
    }, async (probe) => {
      const result = await probe({ processHandle });
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("就绪前进程退出: exit=7 signal=null");
    });
  });

  test("已退出进程不发送健康请求", async () => {
    await withHttpProbe(() => {}, async (probe, observed) => {
      const result = await probe({ processHandle: { exitCode: null, signalCode: "SIGTERM" } });
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("signal=SIGTERM");
      expect(observed.requests).toBe(0);
    });
  });

  test("系统校时不提前结束健康重试预算", async () => {
    const wallNow = Date.now.bind(Date);
    let clock;
    try {
      await withHttpProbe((_request, response, observed) => {
        if (observed.requests === 1) clock = spyOn(Date, "now").mockImplementation(() => wallNow() + 60_000);
        response.statusCode = observed.requests === 1 ? 503 : 200;
        response.end();
      }, async (probe, observed) => {
        expect(await probe({ timeoutMs: 1000 })).toEqual({ ready: true });
        expect(observed.requests).toBe(2);
      });
    } finally {
      clock?.mockRestore();
    }
  });

  test("仅需状态码的探针取消未消费的响应正文", async () => {
    await withHttpProbe((_request, response) => {
      response.writeHead(200);
      response.write("unneeded streaming body");
    }, async (probe, observed) => {
      expect(await probe()).toEqual({ ready: true });
      await Bun.sleep(20);
      expect(observed.closedConnections).toBe(1);
    });
  });
});

async function withDetachedProbe(exercise) {
  const runtime = path.join(runtimeRoot, randomUUID());
  mkdirSync(runtime, { recursive: true });
  const readyFile = path.join(runtime, "ready.json");
  const child = { pid: 12345, exitCode: null, signalCode: null };
  try {
    await exercise({ child, readyFile, logPath: path.join(runtime, "manager.log"), timeoutMs: 60 });
  } finally {
    rmSync(runtime, { recursive: true, force: true });
  }
}

describe("detached manager就绪记录", () => {
  test("只接受当前子进程PID发布的就绪记录", async () => {
    await withDetachedProbe(async (options) => {
      writeFileSync(options.readyFile, JSON.stringify({ pid: options.child.pid }));
      await waitForDetachedReady(options);
    });
  });

  test("旧PID记录不能报告成功，轮询受剩余预算限制", async () => {
    await withDetachedProbe(async (options) => {
      writeFileSync(options.readyFile, JSON.stringify({ pid: 54321 }));
      const started = performance.now();
      await expect(waitForDetachedReady(options)).rejects.toThrow("在 60ms 内未就绪");
      expect(performance.now() - started).toBeLessThan(220);
    });
  });

  test("尚未写完的JSON允许下一次完整发布", async () => {
    await withDetachedProbe(async (options) => {
      writeFileSync(options.readyFile, "{");
      const timer = setTimeout(() => writeFileSync(options.readyFile, JSON.stringify({ pid: options.child.pid })), 20);
      try {
        await waitForDetachedReady({ ...options, timeoutMs: 1000 });
      } finally {
        clearTimeout(timer);
      }
    });
  });

  test("进程退出保留退出事实和日志位置", async () => {
    await withDetachedProbe(async (options) => {
      options.child.exitCode = 3;
      await expect(waitForDetachedReady(options)).rejects.toThrow(`exit=3 signal=null，日志: ${options.logPath}`);
    });
  });
});
