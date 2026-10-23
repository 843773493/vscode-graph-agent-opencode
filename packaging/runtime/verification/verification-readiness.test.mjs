import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const fixtureRunId = randomUUID();

async function observeReadiness(moduleUrl, scenario) {
  const { default: assert } = await import("node:assert/strict");
  const { createServer } = await import("node:http");
  const { setTimeout: sleep } = await import("node:timers/promises");
  const { waitForInstalledGateway } = await import(moduleUrl);
  const child = { exitCode: scenario === "exit-before" ? 7 : null,
    signalCode: scenario === "signal-before" ? "SIGTERM" : null };
  const sockets = new Set(), unfinishedResponses = new Set();
  let requests = 0, timer, delayedResponse, failure, clockShiftMs = 0;
  const originalNow = Date.now;
  const server = createServer((request, response) => {
    requests++;
    assert.equal(request.headers["x-probe"], "fixture");
    unfinishedResponses.add(response);
    response.once("finish", () => unfinishedResponses.delete(response));
    request.socket.once("close", () => unfinishedResponses.delete(response));
    if (scenario === "hung") return;
    if (scenario === "late-ready") {
      delayedResponse = setTimeout(() => response.end("ready"), 150);
      return;
    }
    if (scenario === "exit-during") child.exitCode = 9;
    if (scenario === "rejected-stream" || ["retry", "wall-clock"].includes(scenario) && requests === 1) {
      response.statusCode = 503;
      if (scenario === "wall-clock") clockShiftMs = 3_600_000;
    }
    if (["ready-stream", "rejected-stream"].includes(scenario)) response.write("health");
    else response.end("health");
  });
  server.on("connection", (socket) => { sockets.add(socket); socket.once("close", () => sockets.delete(socket)); });
  try {
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
    Date.now = () => originalNow() + clockShiftMs;
    const timeoutMs = ["retry", "wall-clock"].includes(scenario) ? 400 : 75;
    const started = performance.now();
    try {
      await Promise.race([
        waitForInstalledGateway({ child, url: `http://127.0.0.1:${server.address().port}/health`,
          headers: { "X-Probe": "fixture" }, timeoutMs }),
        new Promise((_resolve, reject) => { timer = setTimeout(() => reject(new Error("fixture超过600ms")), 600); }),
      ]);
    } catch (error) { failure = error; }
    finally { clearTimeout(timer); }
    const durationMs = performance.now() - started;
    if (["hung", "late-ready", "rejected-stream"].includes(scenario)) {
      assert.match(failure?.message ?? "", /安装版 Gateway 未就绪/);
      assert(failure.cause instanceof Error, "失败保留最后请求原因");
      assert(durationMs < 350, `75ms预算未约束等待: ${durationMs}`);
    } else if (["exit-before", "exit-during", "signal-before"].includes(scenario)) {
      assert.match(failure?.message ?? "", /Launcher 提前退出/);
      assert.match(failure.message, new RegExp(scenario === "signal-before" ? "SIGTERM" : scenario === "exit-before" ? "7" : "9"));
      assert.equal(requests, scenario === "exit-during" ? 1 : 0);
    } else {
      assert.equal(failure, undefined);
      assert.equal(requests, ["retry", "wall-clock"].includes(scenario) ? 2 : 1);
    }
    // 完整响应允许连接复用；只要求未结束响应被消费或取消，空闲连接统一清理。
    const releaseDeadline = performance.now() + 200;
    while (unfinishedResponses.size > 0 && performance.now() < releaseDeadline) await sleep(5);
    assert.equal(unfinishedResponses.size, 0, "状态码探针必须结束或取消本次响应");
    return { scenario, requests, durationMs, error: failure?.message ?? null };
  } finally {
    Date.now = originalNow;
    clearTimeout(timer);
    clearTimeout(delayedResponse);
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

describe("安装验证的Gateway就绪等待", () => {
  for (const scenario of ["ready-stream", "retry", "hung", "rejected-stream", "exit-before",
    "signal-before", "exit-during", "late-ready", "wall-clock"]) {
    test(`${scenario}遵守单调预算、进程归属和请求生命周期`, () => {
      const fixtureRoot = path.resolve(
        "out/tests/packaging/runtime/verification/verification-readiness/workspace", fixtureRunId, scenario,
      );
      const artifactRoot = path.resolve(
        "out/tests/packaging/runtime/verification/verification-readiness/artifacts", fixtureRunId, scenario,
      );
      mkdirSync(path.dirname(fixtureRoot), { recursive: true });
      mkdirSync(fixtureRoot);
      mkdirSync(path.dirname(artifactRoot), { recursive: true });
      mkdirSync(artifactRoot);
      const input = JSON.stringify([new URL("./verification-readiness.mjs", import.meta.url).href, scenario]);
      const args = ["--input-type=module", "--eval",
        `${observeReadiness.toString()}\nconsole.log(JSON.stringify(await observeReadiness(...JSON.parse(process.argv[1]))));`,
        input,
      ];
      const run = spawnSync("node", args, { cwd: fixtureRoot, timeout: 5000 });
      const stdout = run.stdout ?? Buffer.alloc(0);
      const stderr = run.stderr ?? Buffer.alloc(0);
      writeFileSync(path.join(artifactRoot, "stdout.raw"), stdout, { flag: "wx" });
      writeFileSync(path.join(artifactRoot, "stderr.raw"), stderr, { flag: "wx" });
      const spawnError = run.error && {
        name: run.error.name,
        message: run.error.message,
        code: run.error.code ?? null,
        syscall: run.error.syscall ?? null,
        path: run.error.path ?? null,
      };
      writeFileSync(path.join(artifactRoot, "result.json"), `${JSON.stringify({
        scenario,
        cwd: fixtureRoot,
        command: "node",
        args: ["--input-type=module", "--eval", "<embedded observer>", input],
        status: run.status,
        signal: run.signal,
        error: spawnError ?? null,
      }, null, 2)}\n`, { flag: "wx" });
      if (run.error || run.status !== 0) {
        throw new Error(
          `Gateway就绪场景 ${scenario} 失败；cwd=${fixtureRoot}；artifacts=${artifactRoot}；status=${run.status}；signal=${run.signal}；spawnError=${run.error?.message ?? "无"}；stderr:\n${stderr.toString("utf8")}`,
          { cause: run.error },
        );
      }
      expect(JSON.parse(stdout.toString("utf8")).scenario).toBe(scenario);
    });
  }
});
