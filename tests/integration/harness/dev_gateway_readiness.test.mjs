import { describe, expect, spyOn, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";

import { waitForGateway } from "../../../scripts/launch/dev-systemd.mjs";

async function withGatewayProbe(respond, exercise, { managerReady = true } = {}) {
  const runtimeRoot = path.resolve(
    "out/tests/integration/harness/dev_gateway_readiness/runtime",
  );
  mkdirSync(runtimeRoot, { recursive: true });
  const runtime = mkdtempSync(path.join(runtimeRoot, "probe-"));
  const readyFile = path.join(runtime, "ready.json");
  if (managerReady) writeFileSync(readyFile, JSON.stringify({ pid: 12345 }));
  const observed = { requests: 0, closedRequests: 0, mainPid: 12345, activeState: "active" };
  const stateProbe = spyOn(Bun, "spawnSync").mockImplementation((command) => {
    expect(command.slice(0, 4)).toEqual(["systemctl", "--user", "show", "isolated-probe.service"]);
    return {
      exitCode: 0,
      stdout: Buffer.from(`LoadState=loaded\nActiveState=${observed.activeState}\nSubState=running\nMainPID=${observed.mainPid}\n`),
      stderr: Buffer.alloc(0),
    };
  });
  const pending = [];
  const server = createServer((request, response) => {
    observed.requests += 1;
    request.on("close", () => { observed.closedRequests += 1; });
    respond(request, response, observed);
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const unit = {
      serviceName: "isolated-probe.service",
      readyFile,
      gatewayHealthUrl: `http://127.0.0.1:${server.address().port}/health`,
    };
    const probe = (budgetMs) => {
      const result = waitForGateway(unit, budgetMs).then(
        () => ({ ready: true }),
        (error) => ({ ready: false, error }),
      );
      pending.push(result);
      return result;
    };
    await exercise(probe, observed);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await Promise.all(pending);
    stateProbe.mockRestore();
    rmSync(runtime, { recursive: true, force: true });
  }
}

describe("开发 Gateway 就绪 HTTP 预算", () => {
  test("挂起的 HTTP 请求会在就绪预算内取消", async () => {
    await withGatewayProbe(() => {}, async (probe, observed) => {
      const result = await Promise.race([
        probe(100),
        Bun.sleep(800).then(() => ({ stillWaiting: true })),
      ]);
      expect(result.stillWaiting).toBeUndefined();
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("在 100ms 内未就绪");
      expect(observed.requests).toBe(1);
      await Bun.sleep(10);
      expect(observed.closedRequests).toBe(1);
    });
  });

  test("超过期限才返回的成功响应不能报告已就绪", async () => {
    await withGatewayProbe((request, response) => {
      const timer = setTimeout(() => response.end("late"), 250);
      request.on("close", () => clearTimeout(timer));
    }, async (probe, observed) => {
      const result = await probe(100);
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("在 100ms 内未就绪");
      expect(observed.requests).toBe(1);
    });
  });

  test("期限内的 HTTP 拒绝允许后续健康请求成功", async () => {
    await withGatewayProbe((_request, response, observed) => {
      response.statusCode = observed.requests === 1 ? 503 : 200;
      response.end();
    }, async (probe, observed) => {
      expect(await probe(1000)).toEqual({ ready: true });
      expect(observed.requests).toBe(2);
    });
  });

  test("持续 HTTP 拒绝保留最后状态", async () => {
    await withGatewayProbe((_request, response) => {
      response.statusCode = 503;
      response.end();
    }, async (probe, observed) => {
      const result = await probe(100);
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("HTTP 503");
      expect(observed.requests).toBe(1);
    });
  });

  test("manager 未发布就绪记录时不请求健康接口", async () => {
    await withGatewayProbe(() => {}, async (probe, observed) => {
      const result = await probe(100);
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("manager 尚未就绪");
      expect(observed.requests).toBe(0);
    }, { managerReady: false });
  });

  test("健康响应后 manager 已替换不能报告成功", async () => {
    await withGatewayProbe((_request, response, observed) => {
      observed.mainPid = 54321;
      response.end();
    }, async (probe) => {
      const result = await probe(100);
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("manager 已退出或更换");
    });
  });

  test("manager 已退出时立即拒绝且不发送健康请求", async () => {
    await withGatewayProbe(() => {}, async (probe, observed) => {
      observed.activeState = "inactive";
      const result = await probe(1000);
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("就绪前已退出或被回收");
      expect(observed.requests).toBe(0);
    });
  });

  test("后续manager未就绪观察不能覆盖健康请求发现的替换事实", async () => {
    await withGatewayProbe((_request, response, observed) => {
      observed.mainPid = 54321;
      response.end();
    }, async (probe, observed) => {
      const result = await probe(500);
      expect(result.ready).toBe(false);
      expect(result.error.message).toContain("manager 已退出或更换");
      expect(observed.requests).toBe(1);
    });
  });
});
