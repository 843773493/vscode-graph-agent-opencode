import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import {
  acquireLauncherLock,
  readLauncherLock,
} from "./instance-lock.mjs";

const fixtureRoot = path.resolve("out/tests/packages/launcher/src/instance-lock");
const runId = randomUUID();
const fixtureRunRoot = path.join(fixtureRoot, "workspace", runId);
const artifactsRunRoot = path.join(fixtureRoot, "artifacts", runId);
mkdirSync(path.dirname(fixtureRunRoot), { recursive: true });
mkdirSync(fixtureRunRoot);
mkdirSync(path.dirname(artifactsRunRoot), { recursive: true });
mkdirSync(artifactsRunRoot);

const runtime = {
  distribution: "npm",
  version: "test-version",
  manifestPath: path.join(fixtureRunRoot, "runtime", "runtime-manifest.json"),
};

function createLockFixture(scenario) {
  const boxteamHome = path.join(fixtureRunRoot, scenario, "控制 home");
  const artifacts = path.join(artifactsRunRoot, scenario);
  mkdirSync(path.dirname(boxteamHome));
  mkdirSync(boxteamHome);
  mkdirSync(artifacts);
  writeFileSync(path.join(artifacts, "input.json"), `${JSON.stringify({
    boxteamHome, runtime,
  }, null, 2)}\n`, { flag: "wx" });
  return { boxteamHome, artifacts };
}

function retainLockSnapshot(fixture, lockPath, phase) {
  const bytes = readFileSync(lockPath);
  writeFileSync(path.join(fixture.artifacts, `${phase}.lock.json`), bytes, { flag: "wx" });
  return bytes;
}

describe("launcher instance lock", () => {
  test("同一个 home 拒绝活动实例", () => {
    const fixture = createLockFixture("active-conflict");
    const { boxteamHome } = fixture;
    const first = acquireLauncherLock({
      boxteamHome,
      runtime,
      processObject: { pid: 1001 },
      killImpl() {},
    });

    retainLockSnapshot(fixture, first.path, "original");
    expect(() =>
      acquireLauncherLock({
        boxteamHome,
        runtime,
        processObject: { pid: 1002 },
        killImpl() {},
      }),
    ).toThrow("pid=1001");
    retainLockSnapshot(fixture, first.path, "after-conflict");
    first.release();
  });

  test("确认旧 pid 不存在后替换失效锁", () => {
    const fixture = createLockFixture("stale-replacement");
    const { boxteamHome } = fixture;
    const original = acquireLauncherLock({
      boxteamHome,
      runtime,
      processObject: { pid: 1001 },
      killImpl() {},
    });
    retainLockSnapshot(fixture, original.path, "original");

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

    retainLockSnapshot(fixture, replacement.path, "replacement");
    expect(readLauncherLock(boxteamHome).value.pid).toBe(1002);
    replacement.release();
    expect(readLauncherLock(boxteamHome)).toBeNull();
  });

  for (const failure of [
    new TypeError("进程查询接线错误"),
    new RangeError("进程查询参数越界"),
    Object.assign(new Error("进程查询IO失败"), { code: "EIO" }),
  ]) {
    test(`查询异常${failure.name}/${failure.code ?? "无错误码"}原样交付并保留原锁`, () => {
      const fixture = createLockFixture(`query-${failure.code ?? failure.name}`);
      const { boxteamHome } = fixture;
      const first = acquireLauncherLock({
        boxteamHome, runtime, processObject: { pid: 1001 }, killImpl() {},
      });
      const originalBytes = retainLockSnapshot(fixture, first.path, "original");
      let observed;
      try {
        acquireLauncherLock({
          boxteamHome, runtime, processObject: { pid: 1002 },
          killImpl() { throw failure; },
        });
      } catch (error) { observed = error; }
      const afterBytes = retainLockSnapshot(fixture, first.path, "after-query");
      expect(observed).toBe(failure);
      expect(afterBytes).toEqual(originalBytes);
      first.release();
    });
  }

  test("权限不足仍按活动实例拒绝获取，不删除原锁", () => {
    const fixture = createLockFixture("query-EPERM");
    const { boxteamHome } = fixture;
    const first = acquireLauncherLock({
      boxteamHome, runtime, processObject: { pid: 1001 }, killImpl() {},
    });
    const originalBytes = retainLockSnapshot(fixture, first.path, "original");
    expect(() => acquireLauncherLock({
      boxteamHome, runtime, processObject: { pid: 1002 },
      killImpl() { throw Object.assign(new Error("无权查询"), { code: "EPERM" }); },
    })).toThrow("pid=1001");
    expect(retainLockSnapshot(fixture, first.path, "after-query")).toEqual(originalBytes);
    first.release();
  });
});
