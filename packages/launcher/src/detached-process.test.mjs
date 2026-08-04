import { afterEach, describe, expect, test } from "bun:test";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  terminateDetachedProcess,
  terminateProcessWithEscalation,
} from "./detached-process.mjs";

const temporaryRoots = [];
const detachedPids = [];

function temporaryRoot() {
  const root = mkdtempSync(path.join(os.tmpdir(), "boxteam-detached-"));
  temporaryRoots.push(root);
  return root;
}

async function waitForFile(filePath) {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (existsSync(filePath)) return;
    await Bun.sleep(50);
  }
  throw new Error(`等待文件超时: ${filePath}`);
}

afterEach(() => {
  for (const pid of detachedPids.splice(0)) {
    terminateDetachedProcess(pid);
  }
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("detached process", () => {
  test("拒绝无效 pid", () => {
    expect(() => terminateDetachedProcess(0)).toThrow(/pid 无效/);
  });

  test.skipIf(process.platform === "win32")(
    "启动者退出后 detached 子进程仍继续运行",
    async () => {
      const root = temporaryRoot();
      const markerPath = path.join(root, "ready.txt");
      const pidPath = path.join(root, "pid.txt");
      const logPath = path.join(root, "services.log");
      const workerPath = path.join(root, "worker.mjs");
      const launcherPath = path.join(root, "launcher.mjs");
      const helperUrl = new URL("./detached-process.mjs", import.meta.url).href;
      writeFileSync(
        workerPath,
        `import { writeFileSync } from "node:fs";\n` +
          `writeFileSync(${JSON.stringify(markerPath)}, "ready\\n");\n` +
          `setInterval(() => {}, 1000);\n`,
        "utf8",
      );
      writeFileSync(
        launcherPath,
        `import { writeFileSync } from "node:fs";\n` +
          `import { spawnDetachedProcess } from ${JSON.stringify(helperUrl)};\n` +
          `const child = spawnDetachedProcess({` +
          `command: process.execPath, args: [${JSON.stringify(workerPath)}], ` +
          `cwd: ${JSON.stringify(root)}, environment: process.env, ` +
          `logPath: ${JSON.stringify(logPath)}});\n` +
          `writeFileSync(${JSON.stringify(pidPath)}, String(child.pid));\n` +
          `child.unref();\n`,
        "utf8",
      );

      const launcher = Bun.spawn([process.execPath, launcherPath], {
        cwd: root,
        stdout: "ignore",
        stderr: "pipe",
      });
      const exitCode = await launcher.exited;
      expect(exitCode).toBe(0);
      await waitForFile(markerPath);

      const pid = Number.parseInt(readFileSync(pidPath, "utf8"), 10);
      detachedPids.push(pid);
      expect(readFileSync(markerPath, "utf8")).toBe("ready\n");
      expect(statSync(logPath).mode & 0o777).toBe(0o600);
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
