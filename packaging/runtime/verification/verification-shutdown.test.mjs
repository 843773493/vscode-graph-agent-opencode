import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import ts from "typescript";

const source = readFileSync(
  path.resolve("packaging/runtime/verification/verify-windows-x64.mjs"),
  "utf8",
);
const parsed = ts.createSourceFile(
  "verify-windows-x64.mjs",
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.JS,
);
const waitDeclaration = parsed.statements.find(
  (node) =>
    ts.isFunctionDeclaration(node) &&
    node.name?.text === "waitForGatewayStopped",
);
if (!waitDeclaration) {
  throw new Error("verify-windows-x64.mjs 缺少 waitForGatewayStopped");
}

const waitFunctionSource = waitDeclaration.getText(parsed);
const fixtureRunId = randomUUID();

async function observeShutdown({ waitFunctionSource, scenario }) {
  const { default: assert } = await import("node:assert/strict");
  const { createServer } = await import("node:http");
  const vm = await import("node:vm");
  const serverSockets = new Set();
  const unfinishedResponses = new Set();
  const server = createServer((request, response) => {
    requests++;
    if (scenario !== "programming-errors") {
      assert.equal(request.headers["x-probe"], "fixture");
    }
    if (scenario === "hang") {
      unfinishedResponses.add(response);
      response.once("finish", () => unfinishedResponses.delete(response));
      response.once("close", () => unfinishedResponses.delete(response));
      return;
    }
    if (scenario === "reset") {
      request.socket.destroy();
      return;
    }
    unfinishedResponses.add(response);
    response.once("finish", () => unfinishedResponses.delete(response));
    response.once("close", () => unfinishedResponses.delete(response));
    if (scenario === "open-body") {
      response.write("healthy");
      return;
    }
    if (scenario === "wall-clock" && requests === 1) {
      Date.now = () => originalDateNow() - 3_600_000;
    }
    response.end("healthy");
  });
  server.on("connection", (socket) => {
    serverSockets.add(socket);
    socket.once("close", () => serverSockets.delete(socket));
  });

  let requests = 0;
  let serverListening = false;
  const originalDateNow = Date.now;
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  serverListening = true;
  const port = server.address().port;
  if (scenario === "refused") {
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
    serverListening = false;
  }

  const fetchForScenario =
    scenario === "programming-errors"
      ? async (input, options) => {
          if (input === "not a valid URL") return fetch(input, options);
          throw new TypeError("fixture programming error");
        }
      : scenario === "body-refused"
        ? async () => ({
            status: 200,
            body: {
              cancel: async () => {
                const cause = new Error("fixture body connection refused");
                cause.code = "ECONNREFUSED";
                throw new TypeError("fixture body release failed", { cause });
              },
            },
          })
        : fetch;
  const waitForGatewayStopped = vm.runInNewContext(
    `(${waitFunctionSource})`,
    {
      AbortController,
      Date,
      DOMException,
      URL,
      clearTimeout,
      fetch: fetchForScenario,
      gatewayUrl: "http://127.0.0.1",
      headers: {},
      performance,
      setTimeout,
    },
  );
  const timeoutMs =
    scenario === "healthy" ? 620 : scenario === "reset" ? 300 : 120;
  const url = `http://127.0.0.1:${port}/health`;
  const startedAt = performance.now();
  const failures = [];
  let returned = false;
  try {
    if (scenario === "programming-errors") {
      try {
        await waitForGatewayStopped({
          url: "not a valid URL",
          headers: { "X-Probe": "fixture" },
          timeoutMs,
        });
      } catch (error) {
        failures.push(error);
      }
      try {
        await waitForGatewayStopped({
          url,
          headers: { "X-Probe": "fixture" },
          timeoutMs,
        });
      } catch (error) {
        failures.push(error);
      }
    } else {
      try {
        await waitForGatewayStopped({
          url,
          headers: { "X-Probe": "fixture" },
          timeoutMs,
        });
        returned = true;
      } catch (error) {
        failures.push(error);
      }
    }
    const durationMs = performance.now() - startedAt;
    const failure = failures.at(-1);

    if (scenario === "refused") {
      assert.equal(returned, true, "只有loopback连接拒绝表示Gateway已停止");
      assert.equal(failures.length, 0);
    } else if (scenario === "programming-errors") {
      assert.equal(failures.length, 2);
      assert.equal(failures[0].name, "TypeError");
      assert.match(failures[0].message, /Invalid URL|valid URL/i);
      assert.equal(failures[1].name, "TypeError");
      assert.equal(failures[1].message, "fixture programming error");
      assert.equal(requests, 0);
    } else {
      assert.equal(returned, false, `${scenario}不得报告Gateway已停止`);
      assert.match(failure?.message ?? "", /停止观察超时/);
      assert(failure.cause, "停止观察失败必须保留最后原因");
      assert(durationMs < 1_500, `观察超出测试期限: ${durationMs}ms`);
    }

    if (scenario === "body-refused") {
      assert.equal(failure.cause.cause.code, "ECONNREFUSED");
    }
    if (scenario === "healthy") assert(requests >= 2);
    if (["hang", "open-body"].includes(scenario)) {
      const releaseDeadline = performance.now() + 250;
      while (
        unfinishedResponses.size > 0 &&
        performance.now() < releaseDeadline
      ) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
      assert.equal(unfinishedResponses.size, 0, "停止观察必须释放未结束响应");
    }
    if (scenario === "hang") {
      assert.match(failure.cause.message, /健康请求超过停止观察期限/);
    }
    if (scenario === "reset") {
      assert(requests >= 1, "服务器必须实际重置本地健康连接");
    }
    if (scenario === "wall-clock") assert(requests >= 1);

    return {
      scenario,
      returned,
      requests,
      durationMs,
      error: failure?.message ?? null,
      cause: failure?.cause?.message ?? null,
      causeCode: failure?.cause?.cause?.code ?? failure?.cause?.code ?? null,
      openResponses: unfinishedResponses.size,
    };
  } finally {
    Date.now = originalDateNow;
    for (const socket of serverSockets) socket.destroy();
    if (serverListening) {
      await new Promise((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      });
    }
  }
}

function runScenario(scenario) {
  const caseRoot = path.resolve(
    "out/tests/packaging/runtime/verification/verification-shutdown/workspace", fixtureRunId, scenario,
  );
  const artifactRoot = path.resolve(
    "out/tests/packaging/runtime/verification/verification-shutdown/artifacts", fixtureRunId, scenario,
  );
  mkdirSync(path.dirname(caseRoot), { recursive: true });
  mkdirSync(caseRoot);
  mkdirSync(path.dirname(artifactRoot), { recursive: true });
  mkdirSync(artifactRoot);
  const script =
    `${observeShutdown.toString()}\n` +
    "console.log(JSON.stringify(await observeShutdown(JSON.parse(process.argv[1]))));";
  const input = JSON.stringify({ waitFunctionSource, scenario });
  const args = ["--input-type=module", "--eval", script, input];
  const run = spawnSync("node", args, { cwd: caseRoot, timeout: 5_000 });
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
    cwd: caseRoot,
    command: "node",
    args: ["--input-type=module", "--eval", "<embedded observer and production function>", {
      scenario,
      waitFunctionSource: "waitForGatewayStopped from verify-windows-x64.mjs",
    }],
    status: run.status,
    signal: run.signal,
    error: spawnError ?? null,
  }, null, 2)}\n`, { flag: "wx" });
  if (run.error || run.status !== 0) {
    throw new Error(
      `Gateway停止场景 ${scenario} 失败；cwd=${caseRoot}；artifacts=${artifactRoot}；status=${run.status}；signal=${run.signal}；spawnError=${run.error?.message ?? "无"}；stderr:\n${stderr.toString("utf8")}`,
      { cause: run.error },
    );
  }
  return JSON.parse(stdout.toString("utf8"));
}

describe("Windows Launcher 结束后的 Gateway 停止观察", () => {
  test("持续健康响应直到观察预算结束仍然失败", () => {
    const result = runScenario("healthy");
    expect(result.returned).toBe(false);
    expect(result.error).toContain("Gateway 停止观察超时");
    expect(result.requests).toBeGreaterThanOrEqual(2);
  });

  test("挂起健康请求超时并保留取消原因", () => {
    const result = runScenario("hang");
    expect(result.returned).toBe(false);
    expect(result.error).toContain("Gateway 停止观察超时");
    expect(result.cause).toContain("健康请求超过停止观察期限");
    expect(result.openResponses).toBe(0);
  });

  test("未结束的健康正文在预算结束前释放", () => {
    const result = runScenario("open-body");
    expect(result.returned).toBe(false);
    expect(result.error).toContain("Gateway 停止观察超时");
    expect(result.openResponses).toBe(0);
  });

  test("仅loopback连接拒绝符合停止观察语义", () => {
    const result = runScenario("refused");
    expect(result.returned).toBe(true);
    expect(result.error).toBeNull();
  });

  test("连接重置不表示Gateway已停止", () => {
    const result = runScenario("reset");
    expect(result.returned).toBe(false);
    expect(result.error).toContain("Gateway 停止观察超时");
    expect(result.cause).toBeTruthy();
  });

  test("墙钟回拨不延长单调观察预算", () => {
    const result = runScenario("wall-clock");
    expect(result.returned).toBe(false);
    expect(result.error).toContain("Gateway 停止观察超时");
    expect(result.durationMs).toBeLessThan(1_500);
  });

  test("非法URL和未知TypeError原样失败", () => {
    const result = runScenario("programming-errors");
    expect(result.returned).toBe(false);
    expect(result.error).toContain("fixture programming error");

    const bodyRefused = runScenario("body-refused");
    expect(bodyRefused.returned).toBe(false);
    expect(bodyRefused.error).toContain("Gateway 停止观察超时");
    expect(bodyRefused.causeCode).toBe("ECONNREFUSED");
  });
});
