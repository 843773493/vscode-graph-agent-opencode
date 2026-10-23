import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const outputRoot = path.resolve("out/tests/packages/launcher/src/instance-lock-errors");
const runId = randomUUID();
const fixtureRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}
const moduleUrl = new URL("./instance-lock.mjs", import.meta.url).href;

// 每次运行只保存一份观察脚本，各场景只传自己的固定输入。
const observerPath = path.join(artifactsRunRoot, "observer.mjs");
writeFileSync(observerPath, `import { readFileSync } from "node:fs";\n${observeLockAcquisitionFailure.toString()}\nconsole.log(JSON.stringify(await observeLockAcquisitionFailure(...JSON.parse(readFileSync(process.argv[2], "utf8")))));\n`, { flag: "wx" });

async function observeLockAcquisitionFailure(moduleUrl, fixtureHome, scenario) {
  const { default: fs } = await import("node:fs");
  const { default: path } = await import("node:path");
  const { syncBuiltinESMExports } = await import("node:module");
  const original = { ...fs };
  const lockPath = path.join(fixtureHome, "state", "launcher.lock");
  const writeError = Object.assign(new Error("部分写入后失败"), { code: "EIO" });
  const closeError = Object.assign(new Error("关闭锁描述符失败"), { code: "EIO" });
  const removeError = Object.assign(new Error("删除未完成锁失败"), { code: "EACCES" });
  let firstAttempt = true;
  let lockDescriptor;
  let closeAttempts = 0;
  let removeAttempts = 0;
  fs.openSync = (file, ...args) => {
    const descriptor = original.openSync(file, ...args);
    if (firstAttempt && String(file) === lockPath) lockDescriptor = descriptor;
    return descriptor;
  };
  fs.writeFileSync = (file, ...args) => {
    if (firstAttempt && file === lockDescriptor && scenario.includes("write")) {
      original.writeFileSync(file, "{", "utf8");
      throw writeError;
    }
    return original.writeFileSync(file, ...args);
  };
  fs.closeSync = (descriptor) => {
    if (firstAttempt && descriptor === lockDescriptor) {
      closeAttempts++;
      original.closeSync(descriptor);
      if (scenario.includes("close")) throw closeError;
      return;
    }
    return original.closeSync(descriptor);
  };
  fs.rmSync = (file, ...args) => {
    if (firstAttempt && String(file) === lockPath) {
      removeAttempts++;
      if (scenario.includes("remove")) throw removeError;
    }
    return original.rmSync(file, ...args);
  };
  syncBuiltinESMExports();
  const { acquireLauncherLock } = await import(moduleUrl);
  const options = {
    boxteamHome: fixtureHome,
    runtime: { distribution: "npm", version: "fixture", manifestPath: "fixture-manifest" },
    processObject: { pid: 12345 }, killImpl() {},
  };
  let error;
  try { acquireLauncherLock(options); } catch (caught) { error = caught; }
  firstAttempt = false;
  const existsAfterFailure = original.existsSync(lockPath);
  let reacquired = false;
  let retryError;
  if (!scenario.includes("remove")) {
    try { const next = acquireLauncherLock(options); reacquired = true; next.release(); }
    catch (caught) { retryError = caught.message; }
  }
  const identify = (candidate) => candidate === writeError ? "write"
    : candidate === closeError ? "close" : candidate === removeError ? "remove" : "other";
  return {
    errorType: error?.name, originalError: identify(error),
    errors: error?.errors?.map(identify), cause: identify(error?.cause),
    existsAfterFailure, closeAttempts, removeAttempts, reacquired, retryError,
  };
}

function observeFailedAcquisition(scenario) {
  const workspace = path.join(fixtureRunRoot, scenario);
  const artifacts = path.join(artifactsRunRoot, scenario);
  mkdirSync(workspace);
  mkdirSync(artifacts);
  const fixtureHome = path.join(workspace, "控制 home");
  mkdirSync(fixtureHome);
  const inputPath = path.join(artifacts, "input.json");
  writeFileSync(inputPath, `${JSON.stringify([moduleUrl, fixtureHome, scenario])}\n`, { flag: "wx" });
  // 独立Node注入文件边界故障；正文仍写入真实文件，不调用宿主进程信号。
  const args = [observerPath, inputPath];
  const result = spawnSync("node", args, { cwd: workspace, timeout: 5000 });
  const stdout = result.stdout ?? Buffer.alloc(0);
  const stderr = result.stderr ?? Buffer.alloc(0);
  writeFileSync(path.join(artifacts, "stdout.raw"), stdout, { flag: "wx" });
  writeFileSync(path.join(artifacts, "stderr.raw"), stderr, { flag: "wx" });
  writeFileSync(path.join(artifacts, "result.json"), `${JSON.stringify({
    command: ["node", ...args], cwd: workspace, timeout_ms: 5000,
    status: result.status, signal: result.signal,
    error: result.error ? { name: result.error.name, message: result.error.message, code: result.error.code ?? null } : null,
  }, null, 2)}\n`, { flag: "wx" });
  if (result.error || result.status !== 0) {
    throw new Error(`Launcher锁故障fixture ${scenario}失败；artifacts=${artifacts}\n${stderr.toString("utf8")}`, { cause: result.error });
  }
  return JSON.parse(stdout.toString("utf8"));
}

describe("Launcher锁初始化失败", () => {
  test("部分写入失败后释放描述符与未完成锁，下一次能重新获取", () => {
    const result = observeFailedAcquisition("write");
    expect(result.originalError).toBe("write");
    expect(result.closeAttempts).toBe(1);
    expect(result.removeAttempts).toBe(1);
    expect(result.existsAfterFailure).toBe(false);
    expect(result.reacquired).toBe(true);
  });

  test("关闭失败不重复关闭同一描述符，并清理未交付的锁", () => {
    const result = observeFailedAcquisition("close");
    expect(result.originalError).toBe("close");
    expect(result.closeAttempts).toBe(1);
    expect(result.removeAttempts).toBe(1);
    expect(result.existsAfterFailure).toBe(false);
    expect(result.reacquired).toBe(true);
  });

  test("写入与关闭均失败时保留两份原异常", () => {
    const result = observeFailedAcquisition("write-close");
    expect(result.errorType).toBe("AggregateError");
    expect(result.errors).toEqual(["write", "close"]);
    expect(result.cause).toBe("write");
    expect(result.closeAttempts).toBe(1);
    expect(result.existsAfterFailure).toBe(false);
    expect(result.reacquired).toBe(true);
  });

  test("写入和清理均失败时诚实报告残留，不掩盖写入原因", () => {
    const result = observeFailedAcquisition("write-remove");
    expect(result.errorType).toBe("AggregateError");
    expect(result.errors).toEqual(["write", "remove"]);
    expect(result.cause).toBe("write");
    expect(result.existsAfterFailure).toBe(true);
    expect(result.removeAttempts).toBe(1);
  });

  test("写入、关闭、清理同时失败时按发生顺序保留全部原因", () => {
    const result = observeFailedAcquisition("write-close-remove");
    expect(result.errorType).toBe("AggregateError");
    expect(result.errors).toEqual(["write", "close", "remove"]);
    expect(result.cause).toBe("write");
    expect(result.closeAttempts).toBe(1);
    expect(result.removeAttempts).toBe(1);
    expect(result.existsAfterFailure).toBe(true);
  });
});
