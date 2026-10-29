import assert from "node:assert/strict";
import test from "node:test";
import { BrowserToolTimeoutError, browserRuntimeFailure, withTimeout } from "./browserRuntime.js";

for (const nativePath of ["/private/browser-runtime", String.raw`C:\private\browser-runtime`, String.raw`\\host\share\browser-runtime`]) {
  test(`原生错误与日志次错均保留: ${nativePath}`, (t) => {
    const original = Object.assign(new Error(`EACCES: ${nativePath}`), { code: "EACCES", statusCode: 503 });
    const sinkError = Object.assign(new Error(`ENOTDIR: ${nativePath}/log`), { code: "ENOTDIR" });
    let attempted;
    t.mock.method(process.stderr, "write", (text) => { attempted = text; throw sinkError; });
    const failure = browserRuntimeFailure({ browserId: "browser_test", pageId: "page_test", stage: "create_page", cause: original });
    assert.equal(failure.message, "浏览器运行时操作失败: browser_id=browser_test, page_id=page_test, stage=create_page, code=EACCES");
    assert.equal(failure.code, "EACCES");
    assert.equal(failure.statusCode, 503);
    assert(failure.cause instanceof AggregateError);
    assert.deepEqual(failure.cause.errors, [original, sinkError]);
    assert.equal(failure.cause.cause, original);
    assert(attempted.includes(original.message));
  });

  test(`直接恢复失败保持原超时合同: ${nativePath}`, (t) => {
    const original = new Error(`恢复失败: ${nativePath}`);
    const sinkError = new Error(`诊断失败: ${nativePath}/log`);
    t.mock.method(process.stderr, "write", () => { throw sinkError; });
    const error = new BrowserToolTimeoutError("脚本", 20);
    error.recordRecoveryFailure(original);
    assert.equal(error.code, "browser_tool_timeout");
    assert.equal(error.timeout_ms, 20);
    assert.equal(error.retryable, true);
    assert.equal(error.recovery, "page_reset_failed");
    assert.equal(error.recovery_error, "浏览器页面重置失败: stage=reset_page");
    assert.equal(error.message, "脚本 超时: 20ms；浏览器页面已重置，可重试；浏览器页面重置失败: stage=reset_page");
    assert.deepEqual(error.cause.errors, [original, sinkError]);
    assert.equal(error.cause.cause, original);
  });
}

test("正常日志保持直接cause与原错误码", (t) => {
  let text;
  t.mock.method(process.stderr, "write", value => { text = value; });
  const original = Object.assign(new Error("/private/original-runtime"), { code: "EPERM" });
  const error = browserRuntimeFailure({ browserId: "browser_test", stage: "acquire_context", cause: original });
  assert.equal(error.cause, original);
  assert.equal(error.code, "EPERM");
  assert(text.includes(original.message));
  const timeout = new BrowserToolTimeoutError("脚本", 20);
  timeout.recordRecoveryFailure(original);
  assert.equal(timeout.cause, original);
});

for (const mode of ["sync", "async"]) {
  test(`${mode}恢复与日志双重失败仍返回原超时`, async (t) => {
    const recoveryError = new Error("/private/failed-reset");
    const sinkError = new Error("/private/failed-log");
    t.mock.method(process.stderr, "write", () => { throw sinkError; });
    let timeout;
    await assert.rejects(withTimeout(new Promise(() => {}), 10, "脚本", {
      onTimeout: error => {
        timeout = error;
        if (mode === "sync") throw recoveryError;
        return Promise.reject(recoveryError);
      },
    }), error => error === timeout && error instanceof BrowserToolTimeoutError);
    // 原异步恢复catch在当前microtask队列完成，不引入轮询或缩短产品默认期限。
    await Promise.resolve();
    assert.equal(timeout.recovery, "page_reset_failed");
    assert.deepEqual(timeout.cause.errors, [recoveryError, sinkError]);
    assert.equal(timeout.code, "browser_tool_timeout");
    assert.equal(timeout.retryable, true);
    assert(!timeout.message.includes("/private/"));
  });
}
