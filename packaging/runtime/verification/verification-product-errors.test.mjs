import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = ts.createSourceFile("verify-windows-x64.mjs",
  readFileSync(new URL("./verify-windows-x64.mjs", import.meta.url), "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const declaration = source.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === "verifyInstalledProduct");

function verification({ readinessError, productError, cleanupError, gatewayStopError,
  exitCode = 0, signalCode = null, output = "launcher output" } = {}) {
  const calls = [];
  const child = { pid: 12345, exitCode, signalCode };
  const readOutput = () => output;
  const options = { nodeExecutable: "/fixture/node.exe", launcherEntry: "/fixture/boxteam.mjs",
    cwd: "/fixture", env: { BOXTEAM_HOME: "/fixture/home" }, pluginArchive: "/fixture/developer-tools.zip" };
  const headers = { "X-Local-Token": "fixture" };
  const context = vm.createContext({
    AggregateError, gatewayUrl: "http://127.0.0.1:8014", gatewayReadyTimeoutMs: 180_000, headers,
    async waitForInstalledGateway(actual) {
      calls.push("ready");
      expect(actual).toEqual({ child, url: "http://127.0.0.1:8014/api/gateway/health", headers, timeoutMs: 180_000 });
      if (readinessError) throw readinessError;
    },
    async verifyRunningProduct(actualOptions) {
      calls.push("product");
      expect(actualOptions).toBe(options);
      if (productError) throw productError;
    },
    async stopLauncher(actualChild, actualOptions) {
      calls.push("stop");
      expect(actualChild).toBe(child);
      expect(actualOptions).toBe(options);
      if (cleanupError) throw cleanupError;
    },
    async waitForGatewayStopped() {
      calls.push("gateway-stopped");
      if (gatewayStopError) throw gatewayStopError;
    },
  });
  vm.runInContext(declaration.getText(source), context);
  return { calls, run: () => context.verifyInstalledProduct(child, readOutput, options) };
}

test("安装验收沿原就绪、业务检查和关闭顺序完成", async () => {
  const run = verification();
  await run.run();
  expect(run.calls).toEqual(["ready", "product", "stop", "gateway-stopped"]);
});

test("业务失败后仍清理，并保留原始错误身份", async () => {
  const productError = new Error("Browser 创建返回 503");
  const run = verification({ productError });
  await expect(run.run()).rejects.toBe(productError);
  expect(run.calls).toEqual(["ready", "product", "stop", "gateway-stopped"]);
});

test("仅清理失败时原样报告，不将验收标为成功", async () => {
  const cleanupError = new Error("stop 命令失败");
  const run = verification({ cleanupError });
  await expect(run.run()).rejects.toBe(cleanupError);
  expect(run.calls).toEqual(["ready", "product", "stop"]);
});

for (const state of [{ exitCode: 1 }, { signalCode: "SIGTERM" }]) {
  test(`原 Launcher 异常退出不能当作停机验收成功：${JSON.stringify(state)}`, async () => {
    const run = verification(state);
    await expect(run.run()).rejects.toThrow("Launcher 关闭结果异常");
    expect(run.calls).toEqual(["ready", "product", "stop"]);
  });
}

test("Launcher 退出后 Gateway 仍未停止，保留原观察错误", async () => {
  const gatewayStopError = new Error("Gateway 仍在运行");
  const run = verification({ gatewayStopError });
  await expect(run.run()).rejects.toBe(gatewayStopError);
});

test("正常退出但服务输出含异常堆栈，不能当作验收成功", async () => {
  const run = verification({ output: "Traceback: fixture shutdown failure" });
  await expect(run.run()).rejects.toThrow("fixture shutdown failure");
});

for (const phase of ["readiness", "product"]) {
  test(`${phase}失败与清理失败同时保留，业务错误在前`, async () => {
    const original = new Error(`${phase} failed`);
    const cleanupError = new Error("stop 命令失败");
    const run = verification({ [`${phase}Error`]: original, cleanupError });
    const error = await run.run().then(() => { throw new Error("预期验收失败"); }, cause => cause);
    expect(error).toBeInstanceOf(AggregateError);
    expect(error.errors).toEqual([original, cleanupError]);
    expect(run.calls).toEqual(phase === "readiness" ? ["ready", "stop"] : ["ready", "product", "stop"]);
  });
}
