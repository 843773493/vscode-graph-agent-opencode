import { afterAll, describe, expect, spyOn, test } from "bun:test";
import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import {
  terminateDetachedProcess,
  terminateProcessWithEscalation,
} from "./detached-process.mjs";

const fixtureRunRoot = path.resolve(
  "out/tests/packages/launcher/src/detached-process/workspace", randomUUID(),
);
afterAll(() => rmSync(fixtureRunRoot, { recursive: true, force: true }));

async function waitForWorkerReadyMarker(readyMarkerPath) {
  const deadlineMonotonicMs = performance.now() + 10_000;
  while (performance.now() < deadlineMonotonicMs) {
    if (existsSync(readyMarkerPath)) return;
    await Bun.sleep(50);
  }
  throw new Error(`等待文件超时: ${readyMarkerPath}`);
}

describe("detached process", () => {
  test("拒绝无效 pid", () => {
    expect(() => terminateDetachedProcess(0)).toThrow(/pid 无效/);
  });

  test(
    "启动者退出后 detached 子进程仍继续运行",
    async () => {
      const caseWorkspacePath = path.join(fixtureRunRoot, randomUUID());
      mkdirSync(caseWorkspacePath, { recursive: true });
      const workerReadyMarkerPath = path.join(caseWorkspacePath, "ready.txt");
      const workerPidRecordPath = path.join(caseWorkspacePath, "pid.txt");
      const logPath = path.join(caseWorkspacePath, "services.log");
      const workerPath = path.join(caseWorkspacePath, "worker.mjs");
      const launcherPath = path.join(caseWorkspacePath, "launcher.mjs");
      const detachedProcessModuleUrl = new URL(
        "./detached-process.mjs",
        import.meta.url,
      ).href;
      let workerPid;
      try {
        writeFileSync(
          workerPath,
          `import { writeFileSync } from "node:fs";\n` +
            `writeFileSync(${JSON.stringify(workerReadyMarkerPath)}, "ready\\n");\n` +
            // 父测试被外部终止时也不能留下永久运行的detached fixture。
            `setTimeout(() => process.exit(0), 15_000);\n`,
          "utf8",
        );
        writeFileSync(
          launcherPath,
          `import { writeFileSync } from "node:fs";\n` +
            `import { spawnDetachedProcess } from ${JSON.stringify(detachedProcessModuleUrl)};\n` +
            `import { loadRuntimeLogContract } from ${JSON.stringify(new URL("../../../src/shared/serverRuntimeLogging.mjs", import.meta.url).href)};\n` +
            `const child = spawnDetachedProcess({contract: loadRuntimeLogContract(), ` +
            `command: process.execPath, args: [${JSON.stringify(workerPath)}], ` +
            `cwd: ${JSON.stringify(caseWorkspacePath)}, environment: process.env, ` +
            `logPath: ${JSON.stringify(logPath)}});\n` +
            `writeFileSync(${JSON.stringify(workerPidRecordPath)}, String(child.pid));\n` +
            `child.unref();\n`,
          "utf8",
        );

        const launcher = Bun.spawn([process.execPath, launcherPath], {
          cwd: caseWorkspacePath,
          env: { ...process.env, BOXTEAM_PROJECT_ROOT: process.cwd() },
          stdout: "ignore",
          stderr: "pipe",
        });
        const exitCode = await launcher.exited;
        expect(exitCode).toBe(0);
        await waitForWorkerReadyMarker(workerReadyMarkerPath);

        workerPid = Number.parseInt(readFileSync(workerPidRecordPath, "utf8"), 10);
        const logStats = statSync(logPath);
        expect(() => process.kill(workerPid, 0)).not.toThrow();
        expect(readFileSync(workerReadyMarkerPath, "utf8")).toBe("ready\n");
        expect(logStats.isFile()).toBe(true);
        if (process.platform !== "win32") {
          expect(logStats.mode & 0o777).toBe(0o600);
        }
      } finally {
        // 从预先固定的PID记录清理，ready等待或断言失败也能发出终止请求。
        try {
          if (workerPid === undefined && existsSync(workerPidRecordPath)) {
            workerPid = Number.parseInt(readFileSync(workerPidRecordPath, "utf8"), 10);
          }
          if (workerPid !== undefined) {
            terminateDetachedProcess(workerPid);
          }
        } finally {
          rmSync(caseWorkspacePath, { recursive: true, force: true });
        }
      }
    },
  );

  test("优雅停止成功时不发送 SIGKILL", async () => {
    let alive = true;
    const signals = [];
    const killImpl = (_pid, signal) => {
      if (signal === 0) {
        if (!alive) {
          throw Object.assign(new Error("不存在"), { code: "ESRCH" });
        }
        return;
      }
      signals.push(signal);
      if (signal === "SIGTERM") alive = false;
    };

    await terminateProcessWithEscalation(1234, {
      gracefulTimeoutMs: 10,
      forceTimeoutMs: 10,
      pollIntervalMs: 1,
      killImpl,
      sleepImpl: async () => {},
    });

    expect(signals).toEqual(["SIGTERM"]);
  });

  test("优雅停止超时后发送 SIGKILL", async () => {
    let alive = true;
    let now = 0;
    const signals = [];
    await terminateProcessWithEscalation(1234, {
      gracefulTimeoutMs: 10,
      forceTimeoutMs: 10,
      pollIntervalMs: 5,
      killImpl(_pid, signal) {
        if (signal === 0) {
          if (!alive) {
            throw Object.assign(new Error("不存在"), { code: "ESRCH" });
          }
          return;
        }
        signals.push(signal);
        if (signal === "SIGKILL") alive = false;
      },
      sleepImpl: async (delayMs) => {
        now += delayMs;
      },
      nowImpl: () => now,
    });

    expect(signals).toEqual(["SIGTERM", "SIGKILL"]);
  });
});

describe("退出等待的单调预算", () => {
  test("墙钟前跳不会提前把优雅终止升级为强杀", async () => {
    let alive = true;
    let polls = 0;
    let wallOffsetMs = 0;
    const signals = [];
    const originalWallNow = Date.now.bind(Date);
    // 默认参数在调用时绑定，须提前安装才能覆盖旧实现。
    const clock = spyOn(Date, "now").mockImplementation(() => originalWallNow() + wallOffsetMs);
    try {
      await terminateProcessWithEscalation(1234, {
        gracefulTimeoutMs: 100,
        forceTimeoutMs: 20,
        pollIntervalMs: 1,
        killImpl(_pid, signal) {
          if (signal === 0) {
            if (!alive) throw Object.assign(new Error("已退出"), { code: "ESRCH" });
            return;
          }
          signals.push(signal);
          if (signal === "SIGKILL") alive = false;
        },
        sleepImpl: async () => {
          polls++;
          wallOffsetMs += 60_000;
          if (polls === 2) alive = false;
        },
      });
      expect(signals).toEqual(["SIGTERM"]);
      expect(polls).toBe(2);
    } finally {
      clock.mockRestore();
    }
  });

  test("墙钟回拨不会延长强制升级前的等待", async () => {
    let alive = true;
    let polls = 0;
    let wallOffsetMs = 0;
    const signals = [];
    const originalWallNow = Date.now.bind(Date);
    const clock = spyOn(Date, "now").mockImplementation(() => originalWallNow() + wallOffsetMs);
    try {
      await terminateProcessWithEscalation(1234, {
        gracefulTimeoutMs: 30,
        forceTimeoutMs: 20,
        pollIntervalMs: 5,
        killImpl(_pid, signal) {
          if (signal === 0) {
            if (!alive) throw Object.assign(new Error("已退出"), { code: "ESRCH" });
            return;
          }
          signals.push(signal);
          if (signal === "SIGKILL") alive = false;
        },
        sleepImpl: async (delayMs) => {
          wallOffsetMs -= 60_000;
          if (++polls > 20) throw new Error("回拨导致等待超过原预算");
          await Bun.sleep(delayMs);
        },
      });
      expect(signals).toEqual(["SIGTERM", "SIGKILL"]);
      expect(polls).toBeLessThanOrEqual(20);
    } finally {
      clock.mockRestore();
    }
  });

  test("耗尽剩余预算时不传入负轮询延迟", async () => {
    let alive = true;
    let monotonicMs = -6;
    const delays = [];
    const signals = [];
    await terminateProcessWithEscalation(1234, {
      gracefulTimeoutMs: 10,
      forceTimeoutMs: 10,
      pollIntervalMs: 100,
      nowImpl: () => (monotonicMs += 6),
      killImpl(_pid, signal) {
        if (signal === 0) {
          if (!alive) throw Object.assign(new Error("已退出"), { code: "ESRCH" });
          return;
        }
        signals.push(signal);
        if (signal === "SIGKILL") alive = false;
      },
      sleepImpl: async (delayMs) => {
        if (delayMs <= 0) throw new Error(`轮询延迟必须为正数: ${delayMs}`);
        delays.push(delayMs);
      },
    });
    expect(delays).toEqual([4]);
    expect(signals).toEqual(["SIGTERM", "SIGKILL"]);
  });
});

test("退出等待向下取整的sleep也不会在亚毫秒余量空转", async () => {
  let alive = true;
  let monotonicMs = 0;
  const delays = [];
  const signals = [];
  await terminateProcessWithEscalation(1234, {
    gracefulTimeoutMs: 0.5,
    forceTimeoutMs: 1,
    pollIntervalMs: 10,
    nowImpl: () => monotonicMs,
    killImpl(_pid, signal) {
      if (signal === 0) {
        if (!alive) throw Object.assign(new Error("已退出"), { code: "ESRCH" });
        return;
      }
      signals.push(signal);
      if (signal === "SIGKILL") alive = false;
    },
    sleepImpl: async (delayMs) => {
      delays.push(delayMs);
      if (delays.length > 3) throw new Error("亚毫秒sleep未推进单调时间");
      monotonicMs += Math.trunc(delayMs);
    },
  });
  expect(delays).toEqual([1]);
  expect(signals).toEqual(["SIGTERM", "SIGKILL"]);
});
