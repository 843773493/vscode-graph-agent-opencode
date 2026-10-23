import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(path.resolve("scripts/launch/dev.mjs"), "utf8");
const parsed = ts.createSourceFile("dev.mjs", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const entry = parsed.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "launchDetachedManager");
if (!entry) throw new Error("dev.mjs缺少launchDetachedManager入口");

async function observeStartup({ startupError, cleanupError, failurePhase = "ready" }) {
  const calls = [];
  const child = { pid: 32109 };
  // 执行实际入口的函数正文，启动、文件和信号均为显式替身；不加载dev的顶层编排。
  const context = {
    path, projectRoot: process.cwd(), boxteamHome: "fixture-home",
    process: { execPath: "fixture-runtime", env: {}, stdout: { write() {} } },
    mkdirSync() {}, rmSync() {}, resolveServiceLogPath: () => "fixture.log",
    spawnDetachedProcess: () => child,
    once: () => failurePhase === "spawn" ? Promise.resolve([startupError]) : new Promise(() => {}),
    waitForDetachedReady: () => failurePhase === "ready" ? Promise.reject(startupError) : new Promise(() => {}),
    terminateDetachedProcess(pid) {
      calls.push(pid);
      if (cleanupError) throw cleanupError;
    },
  };
  const start = vm.runInNewContext(`(${entry.getText(parsed)})`, context);
  try {
    await start();
    throw new Error("失败场景不得返回启动成功");
  } catch (error) {
    return { error, calls };
  }
}

describe("detached开发manager启动失败的清理", () => {
  for (const failurePhase of ["ready", "spawn"]) {
    test(`${failurePhase}失败且终止请求也失败时保留两份原异常`, async () => {
      const startupError = new Error(`${failurePhase}启动失败`);
      const cleanupError = new Error("taskkill拒绝");
      const observed = await observeStartup({ startupError, cleanupError, failurePhase });
      expect(observed.error.name).toBe("AggregateError");
      expect(observed.error.errors).toEqual([startupError, cleanupError]);
      expect(observed.error.errors[0]).toBe(startupError);
      expect(observed.error.errors[1]).toBe(cleanupError);
      expect(observed.error.cause).toBe(startupError);
      expect(observed.calls).toEqual([32109]);
    });

    test(`${failurePhase}失败但清理请求成功时原样抛出启动异常`, async () => {
      const startupError = new Error(`${failurePhase}启动失败`);
      const observed = await observeStartup({ startupError, failurePhase });
      expect(observed.error).toBe(startupError);
      expect(observed.calls).toEqual([32109]);
    });
  }
});
