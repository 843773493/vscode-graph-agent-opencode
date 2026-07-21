import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  acquireLauncherLock,
  readLauncherLock,
} from "./instance-lock.mjs";

const temporaryDirectories = [];

function temporaryHome() {
  const directory = mkdtempSync(path.join(os.tmpdir(), "boxteam-lock-test-"));
  temporaryDirectories.push(directory);
  return directory;
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

const runtime = {
  distribution: "npm",
  version: "0.1.0",
  manifestPath: "/runtime/runtime-manifest.json",
};

describe("launcher instance lock", () => {
  test("同一个 home 拒绝活动实例", () => {
    const boxteamHome = temporaryHome();
    const first = acquireLauncherLock({
      boxteamHome,
      runtime,
      processObject: { pid: 1001 },
      killImpl() {},
    });

    expect(() =>
      acquireLauncherLock({
        boxteamHome,
        runtime,
        processObject: { pid: 1002 },
        killImpl() {},
      }),
    ).toThrow("pid=1001");
    first.release();
  });

  test("确认旧 pid 不存在后替换失效锁", () => {
    const boxteamHome = temporaryHome();
    acquireLauncherLock({
      boxteamHome,
      runtime,
      processObject: { pid: 1001 },
      killImpl() {},
    });

    const replacement = acquireLauncherLock({
      boxteamHome,
      runtime,
      processObject: { pid: 1002 },
      killImpl() {
        const error = new Error("missing");
        error.code = "ESRCH";
        throw error;
      },
    });

    expect(readLauncherLock(boxteamHome).value.pid).toBe(1002);
    replacement.release();
    expect(readLauncherLock(boxteamHome)).toBeNull();
  });
});
