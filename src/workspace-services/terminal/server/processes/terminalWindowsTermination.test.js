import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

const lifecycleModule = new URL("./terminalProcessLifecycle.js", import.meta.url).href;

function observeWindowsTermination(scenario) {
  // 在独立进程中模拟Windows系统边界，绝不向宿主上的真实PID发送信号。
  const source = `
    import assert from "node:assert/strict";
    import childProcess from "node:child_process";
    import { createServer, get } from "node:http";
    import { syncBuiltinESMExports } from "node:module";
    const [moduleUrl, scenario] = JSON.parse(process.argv[1]);
    Object.defineProperty(process, "platform", { value: "win32" });
    const pid = scenario === "invalid" ? 0 : 32109;
    const launchError = new Error("taskkill不存在");
    launchError.code = "ENOENT";
    const toolError = Object.assign(new Error("taskkill失败"), {
      code: 5, signal: null, stderr: "Access is denied",
    });
    const signalError = Object.assign(new Error("taskkill被终止"), {
      code: null, signal: "SIGTERM", stderr: "工具被终止",
    });
    const timeoutError = Object.assign(new Error("taskkill超时"), {
      code: null, killed: true, signal: "SIGTERM", stderr: "工具未在期限内返回",
    });
    let taskkillCalls = 0, existenceChecks = 0, completeTaskkill;
    let toolPending = false, peerRespondedWhilePending = false;
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
      assert.equal(toolPending, false, "taskkill回执前不能开始退出核实");
      existenceChecks++;
      if (scenario === "missing"
          || (scenario === "raced-exit" && taskkillCalls > 0)
          || (scenario === "timeout-raced-exit" && taskkillCalls > 0)
          || (scenario === "pending-tool" && taskkillCalls > 0)
          || (["exits-after-tool", "clock-forward"].includes(scenario) && existenceChecks >= 4)) {
        const error = new Error("进程不存在");
        error.code = "ESRCH";
        throw error;
      }
      return true;
    };
    childProcess.execFile = (command, args, options, callback) => {
      taskkillCalls++;
      assert.equal(command, "taskkill");
      assert.deepEqual(args, ["/T", "/F", "/PID", String(pid)]);
      assert.equal(options.encoding, "utf8");
      assert.equal(options.windowsHide, true);
      assert.equal(options.timeout, 1500);
      const error = scenario === "spawn-error" ? launchError
        : scenario === "signal-error" ? signalError
        : ["timeout-error", "timeout-raced-exit"].includes(scenario) ? timeoutError
        : ["tool-error", "raced-exit"].includes(scenario) ? toolError : null;
      toolPending = true;
      completeTaskkill = () => {
        toolPending = false;
        callback(error, "", error?.stderr ?? "");
      };
      if (scenario !== "pending-tool") setImmediate(completeTaskkill);
    };
    syncBuiltinESMExports();
    const { terminateTerminalProcessTree } = await import(moduleUrl);
    let observation;
    try {
      let result;
      if (scenario === "pending-tool") {
        const server = createServer((request, response) => {
          peerRespondedWhilePending = toolPending;
          response.end("peer-ready");
        });
        await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
        try {
          const termination = terminateTerminalProcessTree({ pid });
          const body = await new Promise((resolve, reject) => {
            get({ hostname: "127.0.0.1", port: server.address().port }, (response) => {
              let text = "";
              response.setEncoding("utf8");
              response.on("data", (chunk) => { text += chunk; });
              response.on("end", () => resolve(text));
              response.on("error", reject);
            }).on("error", reject);
          });
          assert.equal(body, "peer-ready");
          assert.equal(existenceChecks, 1);
          completeTaskkill();
          result = await termination;
        } finally {
          await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
        }
      } else {
        result = await terminateTerminalProcessTree({ pid });
      }
      observation = { result };
    } catch (error) {
      observation = {
        error: error.message,
        original_launch_cause: error.cause === launchError,
        original_tool_cause: error.cause === toolError,
        original_signal_cause: error.cause === signalError,
        original_timeout_cause: error.cause === timeoutError,
      };
    }
    console.log(JSON.stringify({ ...observation, taskkillCalls, existenceChecks, peerRespondedWhilePending }));
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
  assert.equal(observed.original_tool_cause, true);
  assert.equal(observed.result, undefined);
});

test("Windows taskkill被信号结束时不伪报目标不存在", () => {
  const observed = observeWindowsTermination("signal-error");
  assert.match(observed.error, /SIGTERM/);
  assert.match(observed.error, /工具被终止/);
  assert.equal(observed.original_signal_cause, true);
  assert.equal(observed.result, undefined);
});

test("Windows taskkill超时保留原始原因、信号和stderr", () => {
  const observed = observeWindowsTermination("timeout-error");
  assert.match(observed.error, /SIGTERM/);
  assert.match(observed.error, /工具未在期限内返回/);
  assert.equal(observed.original_timeout_cause, true);
  assert.equal(observed.result, undefined);
});

test("Windows taskkill超时后即使根进程消失也不忽略原失败", () => {
  const observed = observeWindowsTermination("timeout-raced-exit");
  assert.equal(observed.original_timeout_cause, true);
  assert.equal(observed.result, undefined);
});

test("Windows taskkill等待回执时另一HTTP请求可以完成", () => {
  const observed = observeWindowsTermination("pending-tool");
  assert.equal(observed.peerRespondedWhilePending, true);
  assert.equal(observed.result, "terminated");
  assert.equal(observed.taskkillCalls, 1);
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
