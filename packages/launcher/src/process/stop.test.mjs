import { describe, expect, test } from "bun:test";
import os from "node:os";
import path from "node:path";

import { stopLauncher } from "./stop.mjs";

const boxteamHome = path.join(os.tmpdir(), "boxteam-stop-test");

function activeLock(token = "lock-token") {
  return {
    path: path.join(boxteamHome, "state", "launcher.lock"),
    value: { token, pid: 1234 },
  };
}

describe("Launcher 停止", () => {
  test("没有锁时不操作进程", async () => {
    const signals = [];
    const result = await stopLauncher({
      boxteamHome,
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
      boxteamHome,
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
      { home: boxteamHome, token: "lock-token" },
    ]);
  });

  test("活动锁对应的进程不存在时只清理锁", async () => {
    let removed = false;
    const result = await stopLauncher({
      boxteamHome,
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
});
