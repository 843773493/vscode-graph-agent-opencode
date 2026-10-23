import { describe, expect, test } from "bun:test";
import { EventEmitter, once } from "node:events";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

const projectRoot = path.resolve();
const fixtureRunId = randomUUID();

function runNodeObservation(scenario, script, input) {
  const outputRoot = path.join(projectRoot, "out/tests/packaging/runtime/verification/verification-shutdown-timers");
  const workspace = path.join(outputRoot, "workspace", fixtureRunId, scenario);
  const artifacts = path.join(outputRoot, "artifacts", fixtureRunId, scenario);
  for (const directory of [workspace, artifacts]) {
    mkdirSync(path.dirname(directory), { recursive: true });
    mkdirSync(directory);
  }
  const scriptPath = path.join(workspace, "observer.mjs");
  const inputPath = path.join(workspace, "input.json");
  writeFileSync(scriptPath, script, { flag: "wx" });
  writeFileSync(inputPath, JSON.stringify(input), { flag: "wx" });
  const args = [scriptPath, inputPath];
  const result = spawnSync("node", args, { cwd: workspace, timeout: 2_500 });
  const stdout = result.stdout ?? Buffer.alloc(0);
  const stderr = result.stderr ?? Buffer.alloc(0);
  writeFileSync(path.join(artifacts, "stdout.raw"), stdout, { flag: "wx" });
  writeFileSync(path.join(artifacts, "stderr.raw"), stderr, { flag: "wx" });
  writeFileSync(path.join(artifacts, "result.json"), `${JSON.stringify({
    scenario, cwd: workspace, command: "node", args,
    status: result.status, signal: result.signal,
    error: result.error ? { name: result.error.name, message: result.error.message, code: result.error.code ?? null } : null,
  }, null, 2)}\n`, { flag: "wx" });
  if (result.error || result.status !== 0) {
    throw new Error(`Launcher关闭观察 ${scenario} 失败: status=${result.status} signal=${result.signal}；artifacts=${artifacts}\n${stderr.toString("utf8")}`, {
      cause: result.error,
    });
  }
  return stdout.toString("utf8").trim();
}

function extractFunction(platform, name) {
  const filename = `packaging/runtime/verification/verify-${platform}-x64.mjs`;
  const source = readFileSync(path.join(projectRoot, filename), "utf8");
  const parsed = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.JS,
  );
  const declaration = parsed.statements.find(
    (node) =>
      ts.isFunctionDeclaration(node) && node.name?.text === name,
  );
  if (!declaration) {
    throw new Error(`${filename} 缺少 ${name}`);
  }
  return declaration.getText(parsed);
}

const functions = {
  linux: {
    stopLauncher: extractFunction("linux", "stopLauncher"),
    verifyRunningProduct: extractFunction("linux", "verifyRunningProduct"),
  },
  windows: {
    stopLauncher: extractFunction("windows", "stopLauncher"),
    verifyRunningProduct: extractFunction("windows", "verifyRunningProduct"),
  },
};

function createTimerHarness() {
  const scheduled = [];
  const cleared = [];
  return {
    scheduled,
    cleared,
    setTimeout(callback, delayMs) {
      const timer = { callback, delayMs };
      scheduled.push(timer);
      return timer;
    },
    clearTimeout(timer) {
      cleared.push(timer);
    },
    fire(timer = scheduled[0]) {
      timer.callback();
    },
    expectCleared() {
      expect(scheduled).toHaveLength(1);
      expect(scheduled[0].delayMs).toBe(45_000);
      expect(cleared).toEqual([scheduled[0]]);
    },
  };
}

function createChild({ exitOnSignal = false } = {}) {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.pid = 42_731;
  child.signals = [];
  child.kill = (signal) => {
    child.signals.push(signal);
    if (exitOnSignal) {
      queueMicrotask(() => {
        child.exitCode = 0;
        child.emit("exit", 0, null);
      });
    }
    return true;
  };
  return child;
}

function makeBaseContext(platform, timers, child, { exitOnTreeKill = false } = {}) {
  const processSignals = [];
  const treeKills = [];
  const requestCalls = [];
  const context = {
    once,
    process: {
      platform,
      kill(pid, signal) {
        processSignals.push([pid, signal]);
      },
    },
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    queueMicrotask,
    signalChild(target, signal) {
      target.kill(signal);
    },
    stopWindowsProcessTree(pid) {
      treeKills.push(pid);
      if (exitOnTreeKill) {
        queueMicrotask(() => {
          child.exitCode = 0;
          child.emit("exit", 0, null);
        });
      }
    },
    waitForGatewayStopped: async () => {},
    gatewayUrl: "http://127.0.0.1:8014",
    headers: {},
    fetch: async (input) => {
      if (String(input) === "http://127.0.0.1:8014/") {
        return { ok: true, text: async () => "root" };
      }
      if (String(input).endsWith("/api/gateway/auth/local-credential")) {
        return { ok: true, json: async () => ({ data: { token: "fixture-token" } }) };
      }
      throw new Error(`未预期的 Gateway 请求: ${String(input)}`);
    },
    requestJson: async (pathname) => {
      requestCalls.push(pathname);
      if (pathname === "/api/gateway/workspaces") {
        return { data: { items: [{ managed: true, workspace_id: "workspace-fixture" }] } };
      }
      if (pathname.includes("/browser-manager/api/browsers")) {
        return { data: { status: "running" } };
      }
      if (pathname.includes("/runtime/restart-safe")) {
        return { data: { status: "restarted" } };
      }
      throw new Error(`未预期的 Gateway API: ${pathname}`);
    },
  };
  return { context, processSignals, treeKills, requestCalls };
}

function evaluate(source, context) {
  return vm.runInNewContext(`(${source})`, context);
}

describe("已结束的 Launcher 不再进入关闭等待", () => {
  for (const [verifier, platform] of [["linux", "linux"], ["linux", "darwin"], ["windows", "win32"], ["windows", "linux"]]) {
    test(`${verifier}验证器在${platform}上识别信号退出和普通退出`, async () => {
      for (const [exitCode, signalCode] of [[null, "SIGTERM"], [7, null]]) {
        const timers = createTimerHarness();
        const child = createChild();
        child.exitCode = exitCode;
        child.signalCode = signalCode;
        const { context, processSignals, treeKills } = makeBaseContext(platform, timers, child);
        const stopping = evaluate(functions[verifier].stopLauncher, context)(child);
        try {
          expect(child.signals).toEqual([]);
          expect(timers.scheduled).toEqual([]);
        } finally {
          // 旧实现会错误地等待45秒；释放替身等待后再交付断言失败。
          for (const timer of timers.scheduled) timers.fire(timer);
          await stopping;
        }
        expect(processSignals).toEqual([]);
        expect(treeKills).toEqual([]);
      }
    });
  }
});

describe("Linux Launcher 关闭计时器", () => {
  test("收到退出事件后清除 45 秒兜底计时器", async () => {
    const timers = createTimerHarness();
    const child = createChild({ exitOnSignal: true });
    const { context } = makeBaseContext("linux", timers, child);
    await evaluate(functions.linux.stopLauncher, context)(child);

    expect(child.signals).toEqual(["SIGTERM"]);
    timers.expectCleared();
  });

  test("等待超时后清除计时器并终止进程组", async () => {
    const timers = createTimerHarness();
    const child = createChild();
    const { context, processSignals } = makeBaseContext("linux", timers, child);
    const stopping = evaluate(functions.linux.stopLauncher, context)(child);
    timers.fire();
    await stopping;

    expect(child.signals).toEqual(["SIGTERM"]);
    expect(processSignals).toEqual([[-child.pid, "SIGKILL"]]);
    timers.expectCleared();
  });
});

describe("Windows Launcher 关闭计时器", () => {
  test("等待超时后清除计时器并结束进程树", async () => {
    const timers = createTimerHarness();
    const child = createChild();
    const { context, treeKills } = makeBaseContext("win32", timers, child);
    const stopping = evaluate(functions.windows.stopLauncher, context)(child);
    timers.fire();
    await stopping;

    expect(child.signals).toEqual(["SIGBREAK"]);
    expect(treeKills).toEqual([child.pid]);
    timers.expectCleared();
  });
});

describe("产品验证关闭计时器", () => {
  test("Linux Launcher 收到 SIGINT 并退出后清除计时器", async () => {
    const timers = createTimerHarness();
    const child = createChild({ exitOnSignal: true });
    const { context, requestCalls } = makeBaseContext("linux", timers, child);
    await evaluate(functions.linux.verifyRunningProduct, context)(
      child,
      () => "",
    );

    expect(child.signals).toEqual(["SIGINT"]);
    expect(requestCalls).toHaveLength(3);
    timers.expectCleared();
  });

  test("Windows 后续 Gateway 停止错误发生前已清除计时器", async () => {
    const timers = createTimerHarness();
    const child = createChild();
    const { context, treeKills } = makeBaseContext("win32", timers, child, {
      exitOnTreeKill: true,
    });
    context.waitForGatewayStopped = async () => {
      expect(timers.cleared).toEqual([timers.scheduled[0]]);
      throw new Error("fixture Gateway 停止观察错误");
    };

    await expect(
      evaluate(functions.windows.verifyRunningProduct, context)(
        child,
        () => "",
        { forceWindowsTreeShutdown: true },
      ),
    ).rejects.toThrow("fixture Gateway 停止观察错误");

    expect(treeKills).toEqual([child.pid]);
    timers.expectCleared();
  });
});

test("真实 45 秒计时器在 Launcher 自然退出后不阻止子进程结束", () => {
  const script = `
    import { EventEmitter, once } from "node:events";
    import { readFileSync } from "node:fs";
    import vm from "node:vm";
    const source = JSON.parse(readFileSync(process.argv[2], "utf8"));
    const child = new EventEmitter();
    child.exitCode = null;
    child.signalCode = null;
    child.pid = 42_731;
    child.kill = () => {
      setImmediate(() => {
        child.exitCode = 0;
        child.emit("exit", 0, null);
      });
    };
    const stopLauncher = vm.runInNewContext("(" + source + ")", {
      once,
      process: { platform: process.platform, kill() {} },
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
    });
    await stopLauncher(child);
    console.log("natural-exit");
  `;
  expect(runNodeObservation("natural-exit", script, functions.linux.stopLauncher)).toBe("natural-exit");
});

test.skipIf(process.platform === "win32")("真实子进程被信号终止后，两验证器均不再发送信号或安排等待", () => {
  const script = `
    import assert from "node:assert/strict";
    import { spawn } from "node:child_process";
    import { once } from "node:events";
    import { readFileSync } from "node:fs";
    import vm from "node:vm";
    const functions = JSON.parse(readFileSync(process.argv[2], "utf8"));
    const child = spawn(process.execPath, ["--eval", 'console.log("ready"); setInterval(() => {}, 1000);'], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    try {
      await once(child.stdout, "data");
      const exited = once(child, "exit");
      assert.equal(child.kill("SIGTERM"), true);
      const [exitCode, signalCode] = await exited;
      assert.equal(exitCode, null);
      assert.equal(signalCode, "SIGTERM");
      const signals = [];
      const timers = [];
      child.kill = (signal) => { signals.push([child.pid, signal]); return false; };
      for (const source of functions) {
        const stopLauncher = vm.runInNewContext("(" + source + ")", {
          once,
          process: { platform: process.platform, kill: (...args) => signals.push(args) },
          signalChild: (target, signal) => target.kill(signal),
          setTimeout: (callback, delayMs) => {
            timers.push(delayMs);
            // 记录真实生产期限，但不为复现已退出状态而等待45秒。
            return setTimeout(callback, 0);
          },
          clearTimeout,
        });
        await stopLauncher(child);
      }
      console.log(JSON.stringify({ pid: child.pid, exitCode, signalCode, signals, timers }));
      assert.deepEqual(signals, []);
      assert.deepEqual(timers, []);
    } finally {
      if (child.exitCode === null && child.signalCode === null) {
        const exited = once(child, "exit");
        child.kill("SIGKILL");
        await exited;
      }
    }
  `;
  const result = JSON.parse(runNodeObservation("already-signaled", script, [functions.linux.stopLauncher, functions.windows.stopLauncher]));
  expect(result.exitCode).toBeNull();
  expect(result.signalCode).toBe("SIGTERM");
  expect(result.signals).toEqual([]);
  expect(result.timers).toEqual([]);
});
