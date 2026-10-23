import { EventEmitter } from "node:events";
import { spawn } from "node:child_process";
import { describe, expect, test } from "bun:test";
import { terminateChildProcessTree } from "../scripts/processTree.js";

function createWindowsTerminationFixture(child) {
  const taskkill = new EventEmitter();
  const calls = [];
  const termination = terminateChildProcessTree(
    { child, name: "backend" },
    {
      platform: "win32",
      spawnProcess: (...args) => {
        calls.push(args);
        return taskkill;
      },
    },
  );
  return { calls, taskkill, termination };
}

describe("角色启动器进程树清理", () => {
  test.each(["linux", "darwin"])("%s 子进程组正常退出时只发送 SIGTERM", async (platform) => {
    const signals = [];
    await terminateChildProcessTree(
      { child: { pid: 42 }, name: "backend" },
      {
        platform,
        signalProcessGroup: (processGroupId, signal) => signals.push([processGroupId, signal]),
        isProcessGroupAlive: () => false,
      },
    );

    expect(signals).toEqual([[42, "SIGTERM"]]);
  });

  test.each(["linux", "darwin"])("%s 在宽限期后对仍存活的进程组发送 SIGKILL", async (platform) => {
    const signals = [];
    let currentTime = 0;
    let processGroupAlive = true;
    await terminateChildProcessTree(
      { child: { pid: 42 }, name: "backend" },
      {
        platform,
        signalProcessGroup: (processGroupId, signal) => {
          signals.push([processGroupId, signal]);
          if (signal === "SIGKILL") {
            processGroupAlive = false;
          }
        },
        isProcessGroupAlive: () => processGroupAlive,
        sleep: async (milliseconds) => {
          currentTime += milliseconds;
        },
        now: () => currentTime,
        processGroupExitTimeoutMs: 20,
        pollIntervalMs: 10,
      },
    );

    expect(signals).toEqual([[42, "SIGTERM"], [42, "SIGKILL"]]);
  });

  test("SIGKILL 后进程组仍存活时报告超时", async () => {
    let currentTime = 0;
    const termination = terminateChildProcessTree(
      { child: { pid: 42 }, name: "backend" },
      {
        platform: "linux",
        signalProcessGroup: () => undefined,
        isProcessGroupAlive: () => true,
        sleep: async (milliseconds) => {
          currentTime += milliseconds;
        },
        now: () => currentTime,
        processGroupExitTimeoutMs: 20,
        pollIntervalMs: 10,
      },
    );

    await expect(termination).rejects.toThrow(
      "无法回收 backend 的进程组: SIGKILL 后 20ms 仍未退出，pid=42",
    );
  });

  test.skipIf(process.platform === "win32")("真实 POSIX detached 进程组及后代忽略 SIGTERM 时整组升级为 SIGKILL", async () => {
    const childScript = [
      "const { spawn } = require('node:child_process');",
      "process.on('SIGTERM', () => {});",
      "const descendant = spawn(process.execPath, ['-e', \"process.on('SIGTERM', () => {}); process.stdout.write('descendant-ready'); setInterval(() => {}, 1000);\"], { stdio: ['ignore', 'pipe', 'inherit'] });",
      "descendant.stdout.once('data', () => process.stdout.write('ready:' + descendant.pid));",
      "setInterval(() => {}, 1000);",
    ].join("\n");
    const child = spawn("node", ["-e", childScript], {
      detached: true,
      stdio: ["ignore", "pipe", "inherit"],
    });
    const processGroupId = child.pid;
    const childExited = new Promise((resolve) => {
      child.once("error", (error) => resolve({ error }));
      child.once("exit", (code, signal) => resolve({ code, signal }));
    });
    let readinessTimeout;
    const ready = new Promise((resolve, reject) => {
      let output = "";
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => {
        output += chunk;
        if (output.includes("ready:")) {
          resolve(output);
        }
      });
      child.once("error", reject);
      readinessTimeout = setTimeout(
        () => reject(new Error("detached 进程及后代未在 2 秒内就绪")),
        2000,
      );
    });

    let processGroupTerminated = false;
    try {
      await ready;
      await terminateChildProcessTree(
        { child, name: "stubborn-fixture" },
        {
          processGroupExitTimeoutMs: 50,
          pollIntervalMs: 10,
        },
      );
      processGroupTerminated = true;
      expect(await childExited).toEqual({ code: null, signal: "SIGKILL" });
    } finally {
      clearTimeout(readinessTimeout);
      if (processGroupId && !processGroupTerminated) {
        try {
          process.kill(-processGroupId, "SIGKILL");
        } catch (error) {
          if (error?.code !== "ESRCH") {
            throw error;
          }
        }
      }
    }
  });

  test("Windows 通过 taskkill 递归回收进程树", async () => {
    const { calls, taskkill, termination } = createWindowsTerminationFixture({
      pid: 42,
      exitCode: null,
      signalCode: null,
    });
    taskkill.emit("exit", 0, null);
    await termination;

    expect(calls).toEqual([[
      "taskkill.exe",
      ["/PID", "42", "/T", "/F"],
      { stdio: "ignore" },
    ]]);
  });

  test("Windows 回收失败时保留详细错误", async () => {
    const { taskkill, termination } = createWindowsTerminationFixture({
      pid: 42,
      exitCode: null,
      signalCode: null,
    });
    taskkill.emit("exit", 1, null);

    await expect(termination).rejects.toThrow(
      "无法回收 backend 的进程树: taskkill code=1 signal=null",
    );
  });

  test("直接子进程已退出时，taskkill 失败仍报告进程树未确认", async () => {
    const { taskkill, termination } = createWindowsTerminationFixture({
      pid: 42,
      exitCode: 0,
      signalCode: null,
    });
    taskkill.emit("exit", 1, null);

    await expect(termination).rejects.toThrow(
      "无法回收 backend 的进程树: taskkill code=1 signal=null",
    );
  });
});
