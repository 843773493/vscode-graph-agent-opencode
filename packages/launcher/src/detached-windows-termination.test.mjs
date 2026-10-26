import { describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";

const moduleUrl = new URL("./detached-process.mjs", import.meta.url).href;

function observeTermination(scenario) {
  // 独立Node进程只替换系统边界；不调用真实taskkill、不向宿主PID发信号。
  const source = `
    import assert from "node:assert/strict";
    import childProcess from "node:child_process";
    import { syncBuiltinESMExports } from "node:module";
    const [moduleUrl, scenario] = JSON.parse(process.argv[1]);
    // 原生库按真实宿主加载；仅终止请求的系统边界模拟 Windows。
    const { terminateDetachedProcess } = await import(moduleUrl);
    Object.defineProperty(process, "platform", { value: "win32" });
    const pid = scenario === "invalid" ? 0 : 32109;
    const launchError = Object.assign(new Error("taskkill启动失败"), { code: "ENOENT" });
    const timeoutError = Object.assign(new Error("taskkill工具超时"), { code: "ETIMEDOUT" });
    const queryError = Object.assign(new Error("无法查询进程"), { code: "EPERM" });
    const calls = [];
    let existenceChecks = 0;
    process.kill = (target, signal) => {
      assert.equal(target, pid);
      assert.equal(signal, 0);
      existenceChecks++;
      if (scenario === "already-gone") throw Object.assign(new Error("进程已消失"), { code: "ESRCH" });
      if (scenario === "query-error") throw queryError;
      return true;
    };
    childProcess.spawnSync = (command, args, options) => {
      assert.equal(command, "taskkill");
      assert.deepEqual(args, ["/T", "/F", "/PID", String(pid)]);
      calls.push(options);
      if (scenario === "launch-error") return { status: null, error: launchError };
      if (scenario === "timeout") return { status: null, signal: "SIGTERM", error: timeoutError, stderr: "工具超时" };
      if (scenario === "signal") return { status: null, signal: "SIGTERM", stderr: "工具被终止" };
      if (scenario === "success") return { status: 0, stderr: "" };
      return { status: 5, stderr: "Access is denied" };
    };
    syncBuiltinESMExports();
    let observation;
    try {
      terminateDetachedProcess(pid);
      observation = { requestReturned: true };
    } catch (error) {
      observation = { error: error.message, errorType: error.name,
        launchCause: error.cause === launchError, timeoutCause: error.cause === timeoutError,
        originalQueryError: error === queryError };
    }
    console.log(JSON.stringify({ ...observation, calls, existenceChecks }));
  `;
  return JSON.parse(execFileSync("node", [
    "--input-type=module", "--eval", source, JSON.stringify([moduleUrl, scenario]),
  ], { encoding: "utf8", timeout: 5000 }));
}

describe("Windows detached进程的终止请求", () => {
  test("无效PID在调用系统工具前拒绝", () => {
    const observed = observeTermination("invalid");
    expect(observed.errorType).toBe("TypeError");
    expect(observed.calls).toEqual([]);
  });

  test("taskkill拒绝且目标仍存在时保留退出码、PID与stderr", () => {
    const observed = observeTermination("tool-error");
    expect(observed.requestReturned).toBeUndefined();
    expect(observed.error).toContain("taskkill");
    expect(observed.error).toContain("pid=32109");
    expect(observed.error).toContain("exit=5");
    expect(observed.error).toContain("Access is denied");
    expect(observed.existenceChecks).toBe(1);
  });

  test("工具被信号终止时不能伪报请求已完成", () => {
    const observed = observeTermination("signal");
    expect(observed.requestReturned).toBeUndefined();
    expect(observed.error).toContain("signal=SIGTERM");
    expect(observed.error).toContain("工具被终止");
  });

  test("工具无法启动时保留原始异常作为cause", () => {
    const observed = observeTermination("launch-error");
    expect(observed.requestReturned).toBeUndefined();
    expect(observed.error).toContain("pid=32109");
    expect(observed.launchCause).toBe(true);
  });

  test("工具超时保留原始超时异常", () => {
    const observed = observeTermination("timeout");
    expect(observed.requestReturned).toBeUndefined();
    expect(observed.timeoutCause).toBe(true);
    expect(observed.error).toContain("taskkill工具超时");
  });

  test("工具非零退出但目标已消失时允许幂等返回", () => {
    const observed = observeTermination("already-gone");
    expect(observed.requestReturned).toBe(true);
    expect(observed.error).toBeUndefined();
    expect(observed.existenceChecks).toBe(1);
  });

  test("查询失败保留原异常而不是当作目标不存在", () => {
    const observed = observeTermination("query-error");
    expect(observed.requestReturned).toBeUndefined();
    expect(observed.originalQueryError).toBe(true);
  });

  test("成功只表示工具返回，调用有界并保留诊断通道", () => {
    const observed = observeTermination("success");
    expect(observed.requestReturned).toBe(true);
    expect(observed.existenceChecks).toBe(0);
    expect(observed.calls).toEqual([{
      encoding: "utf8", stdio: ["ignore", "ignore", "pipe"], windowsHide: true, timeout: 5000,
    }]);
  });
});
