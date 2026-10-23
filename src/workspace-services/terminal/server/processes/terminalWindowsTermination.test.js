import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

const lifecycleModule = new URL("./terminalProcessLifecycle.js", import.meta.url).href;

function observeWindowsTermination(scenario) {
  // 在独立进程中模拟Windows系统边界，绝不向宿主上的真实PID发送信号。
  const source = `
    import assert from "node:assert/strict";
    import childProcess from "node:child_process";
    import { syncBuiltinESMExports } from "node:module";
    const [moduleUrl, scenario] = JSON.parse(process.argv[1]);
    Object.defineProperty(process, "platform", { value: "win32" });
    const pid = scenario === "invalid" ? 0 : 32109;
    const launchError = new Error("taskkill不存在");
    launchError.code = "ENOENT";
    let taskkillCalls = 0, existenceChecks = 0;
    if (scenario.startsWith("clock-")) {
      const wallStart = Date.now(), monotonicStart = performance.now();
      const clockAdjustmentMs = scenario === "clock-forward" ? 3_600_000 : -3_600_000;
      let wallClockReads = 0;
      Date.now = () => wallStart + performance.now() - monotonicStart
        + (wallClockReads++ === 0 ? 0 : clockAdjustmentMs);
    }
    process.kill = (target, signal) => {
      assert.equal(target, pid);
      assert.equal(signal, 0);
      existenceChecks++;
      if (scenario === "missing"
          || (scenario === "raced-exit" && taskkillCalls > 0)
          || (["exits-after-tool", "clock-forward"].includes(scenario) && existenceChecks >= 4)) {
        const error = new Error("进程不存在");
        error.code = "ESRCH";
        throw error;
      }
      return true;
    };
    childProcess.spawnSync = (command, args, options) => {
      taskkillCalls++;
      assert.equal(command, "taskkill");
      assert.deepEqual(args, ["/T", "/F", "/PID", String(pid)]);
      if (scenario === "spawn-error") return { status: null, error: launchError };
      if (scenario === "signal-error") return { status: null, signal: "SIGTERM", stderr: "工具被终止" };
      if (scenario === "tool-error" || scenario === "raced-exit") return { status: 5, stderr: "Access is denied" };
      return { status: 0, stderr: "" };
    };
    syncBuiltinESMExports();
    const { terminateTerminalProcessTree } = await import(moduleUrl);
    let observation;
    try {
      const result = await terminateTerminalProcessTree({ pid });
      observation = { result };
    } catch (error) {
      observation = { error: error.message, original_launch_cause: error.cause === launchError };
    }
    console.log(JSON.stringify({ ...observation, taskkillCalls, existenceChecks }));
  `;
  const stdout = execFileSync(process.execPath, [
    "--input-type=module", "--eval", source, JSON.stringify([lifecycleModule, scenario]),
  ], { encoding: "utf8", timeout: 5000 });
  return JSON.parse(stdout);
}

test("Windows目标已不存在时不调用taskkill", () => {
  const observed = observeWindowsTermination("missing");
  assert.equal(observed.result, "missing");
  assert.equal(observed.taskkillCalls, 0);
});

test("Windows无效PID不调用taskkill", () => {
  const observed = observeWindowsTermination("invalid");
  assert.equal(observed.result, "missing");
  assert.equal(observed.taskkillCalls, 0);
});

test("Windows taskkill无法启动时保留原始异常", () => {
  const observed = observeWindowsTermination("spawn-error");
  assert.match(observed.error, /taskkill/);
  assert.equal(observed.original_launch_cause, true);
  assert.equal(observed.result, undefined);
});

test("Windows taskkill拒绝且进程仍存活时明确报告退出码与stderr", () => {
  const observed = observeWindowsTermination("tool-error");
  assert.match(observed.error, /taskkill/);
  assert.match(observed.error, /5/);
  assert.match(observed.error, /Access is denied/);
  assert.equal(observed.result, undefined);
});

test("Windows taskkill被信号结束时不伪报目标不存在", () => {
  const observed = observeWindowsTermination("signal-error");
  assert.match(observed.error, /SIGTERM/);
  assert.match(observed.error, /工具被终止/);
  assert.equal(observed.result, undefined);
});

test("Windows taskkill成功后仍需观察目标退出", () => {
  const observed = observeWindowsTermination("exits-after-tool");
  assert.equal(observed.result, "terminated");
  assert(observed.existenceChecks >= 4);
  assert.equal(observed.taskkillCalls, 1);
});

test("Windows taskkill成功但目标持续存活时报告still_running", () => {
  const observed = observeWindowsTermination("stays-alive");
  assert.equal(observed.result, "still_running");
  assert(observed.existenceChecks > 2);
  assert.equal(observed.taskkillCalls, 1);
});

test("Windows taskkill非零退出但目标已自行消失时不伪造失败", () => {
  const observed = observeWindowsTermination("raced-exit");
  assert.equal(observed.result, "terminated");
  assert.equal(observed.taskkillCalls, 1);
});

test("系统校时前跳不使宽限期内退出的Windows目标提前超时", () => {
  const observed = observeWindowsTermination("clock-forward");
  assert.equal(observed.result, "terminated");
  assert(observed.existenceChecks >= 4);
});

test("系统校时回拨不延长Windows目标持续存活的退出等待", () => {
  const observed = observeWindowsTermination("clock-backward");
  assert.equal(observed.result, "still_running");
  assert(observed.existenceChecks > 2);
});
