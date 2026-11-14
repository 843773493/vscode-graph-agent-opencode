import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = ts.createSourceFile("verify-windows-x64.mjs",
  readFileSync(new URL("./verify-windows-x64.mjs", import.meta.url), "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const declaration = source.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === "verifyInstalledProduct");

function verification({ readinessError, productError, cleanupError } = {}) {
  const calls = [];
  const child = { pid: 12345 };
  const readOutput = () => "launcher output";
  const options = { forceWindowsTreeShutdown: true, pluginArchive: "/fixture/developer-tools.zip" };
  const headers = { "X-Local-Token": "fixture" };
  const context = vm.createContext({
    AggregateError, gatewayUrl: "http://127.0.0.1:8014", gatewayReadyTimeoutMs: 180_000, headers,
    async waitForInstalledGateway(actual) {
      calls.push("ready");
      expect(actual).toEqual({ child, url: "http://127.0.0.1:8014/api/gateway/health", headers, timeoutMs: 180_000 });
      if (readinessError) throw readinessError;
    },
    async verifyRunningProduct(actualChild, actualOutput, actualOptions) {
      calls.push("product");
      expect([actualChild, actualOutput, actualOptions]).toEqual([child, readOutput, options]);
      if (productError) throw productError;
    },
    async stopLauncher(actualChild) {
      calls.push("stop");
      expect(actualChild).toBe(child);
      if (cleanupError) throw cleanupError;
    },
  });
  vm.runInContext(declaration.getText(source), context);
  return { calls, run: () => context.verifyInstalledProduct(child, readOutput, options) };
}

test("安装验收沿原就绪、业务检查和关闭顺序完成", async () => {
  const run = verification();
  await run.run();
  expect(run.calls).toEqual(["ready", "product", "stop"]);
});

test("业务失败后仍清理，并保留原始错误身份", async () => {
  const productError = new Error("Browser 创建返回 503");
  const run = verification({ productError });
  await expect(run.run()).rejects.toBe(productError);
  expect(run.calls).toEqual(["ready", "product", "stop"]);
});

test("仅清理失败时原样报告，不将验收标为成功", async () => {
  const cleanupError = new Error("taskkill 超时");
  const run = verification({ cleanupError });
  await expect(run.run()).rejects.toBe(cleanupError);
  expect(run.calls).toEqual(["ready", "product", "stop"]);
});

for (const phase of ["readiness", "product"]) {
  test(`${phase}失败与清理失败同时保留，业务错误在前`, async () => {
    const original = new Error(`${phase} failed`);
    const cleanupError = new Error("taskkill 超时");
    const run = verification({ [`${phase}Error`]: original, cleanupError });
    const error = await run.run().then(() => { throw new Error("预期验收失败"); }, cause => cause);
    expect(error).toBeInstanceOf(AggregateError);
    expect(error.errors).toEqual([original, cleanupError]);
    expect(run.calls).toEqual(phase === "readiness" ? ["ready", "stop"] : ["ready", "product", "stop"]);
  });
}
