import { describe, expect, spyOn, test } from "bun:test";
import path from "node:path";

import { stopLauncher } from "./stop.mjs";

const fixtureHome = path.resolve("out/tests/packages/launcher/src/stop/workspace/项目 实例");

function activeLock(token = "lock-token") {
  return {
    path: path.join(fixtureHome, "state", "launcher.lock"),
    value: { token, pid: 1234 },
  };
}

function stoppingFixture() {
  const state = {
    lock: activeLock(), alive: true, monotonicMs: 0,
    signals: [], waits: [], removed: [],
  };
  const options = {
    boxteamHome: fixtureHome,
    readLockImpl: () => state.lock,
    removeLockImpl: (_home, token) => {
      state.removed.push(token);
      state.lock = null;
    },
    isAliveImpl: () => state.alive,
    killImpl: (_pid, signal) => { state.signals.push(signal); },
    sleepImpl: async (milliseconds) => {
      state.waits.push(milliseconds);
      state.monotonicMs += milliseconds;
    },
    nowImpl: () => state.monotonicMs,
    timeoutMs: 25, forceTimeoutMs: 15,
  };
  return { state, options };
}

describe("Launcher 停止", () => {
  test("没有锁时不操作进程", async () => {
    const signals = [];
    const result = await stopLauncher({
      boxteamHome: fixtureHome,
      readLockImpl: () => null,
      killImpl: (...args) => signals.push(args),
    });

    expect(result).toEqual({ status: "not-running" });
    expect(signals).toEqual([]);
  });

  test("优雅停止活动 Launcher 并清理锁", async () => {
    let lock = activeLock();
    let alive = true;
    const signals = [];
    const removed = [];
    let now = 0;
    const result = await stopLauncher({
      boxteamHome: fixtureHome,
      readLockImpl: () => lock,
      removeLockImpl: (home, token) => {
        removed.push({ home, token });
        lock = null;
      },
      isAliveImpl: () => alive,
      killImpl: (pid, signal) => {
        signals.push({ pid, signal });
        alive = false;
      },
      sleepImpl: async () => {
        now += 100;
      },
      nowImpl: () => now,
    });

    expect(result).toEqual({ status: "stopped", pid: 1234 });
    expect(signals).toEqual([{ pid: 1234, signal: "SIGTERM" }]);
    expect(removed).toEqual([
      { home: fixtureHome, token: "lock-token" },
    ]);
  });

  test("活动锁对应的进程不存在时只清理锁", async () => {
    let removed = false;
    const result = await stopLauncher({
      boxteamHome: fixtureHome,
      readLockImpl: () => activeLock(),
      removeLockImpl: () => {
        removed = true;
      },
      isAliveImpl: () => false,
      killImpl: () => {
        throw new Error("不应发送信号");
      },
    });

    expect(result).toEqual({ status: "stale-lock-removed", pid: 1234 });
    expect(removed).toBe(true);
  });

  test("读取锁失败即使包含未退出字样也不触发强杀", async () => {
    const { state, options } = stoppingFixture();
    const failure = new Error("目标未退出，但锁目录读取失败");
    let reads = 0;
    options.readLockImpl = () => {
      if (reads++ > 0) throw failure;
      return state.lock;
    };
    const error = await stopLauncher(options).catch((error) => error);
    expect(error).toBe(failure);
    expect(state.signals).toEqual(["SIGTERM"]);
    expect(state.removed).toEqual([]);
  });

  test("锁消失但原进程仍存活时不能提前报告停止成功", async () => {
    const { state, options } = stoppingFixture();
    options.killImpl = (_pid, signal) => {
      state.signals.push(signal);
      state.lock = null;
      if (signal === "SIGKILL") state.alive = false;
    };
    expect(await stopLauncher(options)).toEqual({ status: "force-stopped", pid: 1234 });
    expect(state.signals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(state.waits).toEqual([25]);
    expect(state.alive).toBe(false);
  });

  test("在优雅期限内退出只等待剩余预算，不追加强杀", async () => {
    const { state, options } = stoppingFixture();
    options.sleepImpl = async (milliseconds) => {
      state.waits.push(milliseconds);
      state.monotonicMs += milliseconds;
      state.alive = false;
    };
    expect(await stopLauncher(options)).toEqual({ status: "stopped", pid: 1234 });
    expect(state.waits).toEqual([25]);
    expect(state.signals).toEqual(["SIGTERM"]);
    expect(state.removed).toEqual(["lock-token"]);
  });

  test("优雅和强制阶段各自超时后明确失败，不延长轮询", async () => {
    const { state, options } = stoppingFixture();
    await expect(stopLauncher(options)).rejects.toThrow("Launcher 在 15ms 内未退出: pid=1234");
    expect(state.signals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(state.waits).toEqual([25, 15]);
    expect(state.removed).toEqual([]);
  });

  test("原进程存活且锁被替换时失败，不强杀或删除新锁", async () => {
    const { state, options } = stoppingFixture();
    const replacement = activeLock("replacement-token");
    options.killImpl = (_pid, signal) => {
      state.signals.push(signal);
      state.lock = replacement;
    };
    await expect(stopLauncher(options)).rejects.toThrow("锁被其它实例替换");
    expect(state.signals).toEqual(["SIGTERM"]);
    expect(state.removed).toEqual([]);
    expect(state.lock).toBe(replacement);
  });

  test("原进程已退出且锁被替换时保留新实例的锁", async () => {
    const { state, options } = stoppingFixture();
    const replacement = activeLock("replacement-token");
    options.killImpl = () => {
      state.alive = false;
      state.lock = replacement;
    };
    expect(await stopLauncher(options)).toEqual({ status: "stopped", pid: 1234 });
    expect(state.removed).toEqual([]);
    expect(state.lock).toBe(replacement);
  });

  test("强杀前目标恰好退出时按ESRCH清理原锁并返回终态", async () => {
    const { state, options } = stoppingFixture();
    options.killImpl = (_pid, signal) => {
      state.signals.push(signal);
      if (signal === "SIGKILL") {
        state.alive = false;
        throw Object.assign(new Error("目标已消失"), { code: "ESRCH" });
      }
    };
    expect(await stopLauncher(options)).toEqual({ status: "force-stopped", pid: 1234 });
    expect(state.signals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(state.removed).toEqual(["lock-token"]);
  });

  test("强杀权限错误原样抛出，不伪报成功或移除活动锁", async () => {
    const { state, options } = stoppingFixture();
    const failure = Object.assign(new Error("终止被拒绝"), { code: "EPERM" });
    options.killImpl = (_pid, signal) => {
      state.signals.push(signal);
      if (signal === "SIGKILL") throw failure;
    };
    expect(await stopLauncher(options).catch((error) => error)).toBe(failure);
    expect(state.removed).toEqual([]);
    expect(state.lock.value.token).toBe("lock-token");
  });

  test("默认停止预算使用单调时间，不受系统校时影响", async () => {
    const { state, options } = stoppingFixture();
    const wallNow = Date.now.bind(Date);
    let wallOffsetMs = 0;
    const clock = spyOn(Date, "now").mockImplementation(() => wallNow() + wallOffsetMs);
    let sleeps = 0;
    delete options.nowImpl;
    options.timeoutMs = 1000;
    options.sleepImpl = async (milliseconds) => {
      await Bun.sleep(milliseconds);
      if (++sleeps === 1) wallOffsetMs = 60_000;
      else state.alive = false;
    };
    try {
      expect(await stopLauncher(options)).toEqual({ status: "stopped", pid: 1234 });
      expect(state.signals).toEqual(["SIGTERM"]);
    } finally {
      clock.mockRestore();
    }
  });

  test("优雅信号遇到ESRCH仍保持既有停止结果", async () => {
    const { state, options } = stoppingFixture();
    options.killImpl = (_pid, signal) => {
      state.signals.push(signal);
      throw Object.assign(new Error("目标已退出"), { code: "ESRCH" });
    };
    expect(await stopLauncher(options)).toEqual({ status: "stopped", pid: 1234 });
    expect(state.removed).toEqual(["lock-token"]);
    expect(state.signals).toEqual(["SIGTERM"]);
  });

  for (const phase of ["停止前", "发送SIGTERM后"]) {
    test(`${phase}默认进程查询失败时保留原错误，不继续发信号或移除锁`, async () => {
      const { state, options } = stoppingFixture();
      const failure = new TypeError("进程查询接线错误");
      let probes = 0;
      // 覆盖实际默认探针；只替换系统边界，不向宿主PID发送信号。
      const kill = spyOn(process, "kill").mockImplementation((_pid, signal) => {
        if (signal === 0) {
          if (phase === "停止前" || probes++ > 0) throw failure;
        } else state.signals.push(signal);
        return true;
      });
      delete options.isAliveImpl;
      delete options.killImpl;
      try {
        expect(await stopLauncher(options).catch((error) => error)).toBe(failure);
        expect(state.signals).toEqual(phase === "停止前" ? [] : ["SIGTERM"]);
        expect(state.removed).toEqual([]);
        expect(state.waits).toEqual([]);
        expect(state.lock.value.token).toBe("lock-token");
      } finally { kill.mockRestore(); }
    });
  }
});
