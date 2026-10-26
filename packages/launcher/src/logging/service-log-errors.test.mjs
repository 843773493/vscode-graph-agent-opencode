import { describe, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const outputRoot = path.resolve("out/tests/packages/launcher/src/logging/service-log-errors");
const runId = randomUUID();
const workspaceRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}
const observerPath = path.join(artifactsRunRoot, "observer.mjs");
writeFileSync(observerPath, `import { readFileSync } from "node:fs";
${observeServiceLogIo.toString()}
console.log(JSON.stringify(await observeServiceLogIo(...JSON.parse(readFileSync(process.argv[2], "utf8")))));
`, { flag: "wx" });

async function observeServiceLogIo(scenario, fixtureRoot, logUrl, detachedUrl, loggingUrl, platform) {
  const { default: assert } = await import("node:assert/strict");
  const { default: fs } = await import("node:fs");
  const { default: childProcess } = await import("node:child_process");
  const { default: path } = await import("node:path");
  const { syncBuiltinESMExports } = await import("node:module");
  const { openServiceLog, SERVICE_LOG_PATH_ENV } = await import(logUrl);
  const { spawnDetachedProcess } = await import(detachedUrl);
  const { loadRuntimeLogContract } = await import(loggingUrl);
  const contract = loadRuntimeLogContract();
  const logPath = path.join(fixtureRoot, "中文 日志.log");
  const originals = Object.fromEntries(["openSync", "chmodSync", "closeSync", "writeSync"].map((key) => [key, fs[key]]));
  const originalSpawn = childProcess.spawn;
  const originalSpawnSync = childProcess.spawnSync;
  const originalKill = process.kill;
  const originalPlatform = process.platform;
  Object.defineProperty(process, "platform", { value: platform });
  const permissionError = new Error("injected chmod failure");
  const closeError = new Error("injected close failure");
  const writeError = new Error("injected write failure");
  const spawnError = new Error("injected spawn failure");
  const terminationError = new Error("injected termination failure");
  const child = scenario === "spawn-close-no-pid" ? {} : { pid: 32109 };
  const events = [];
  let terminationCalls = 0;
  const primaryWrites = [];
  let descriptor, closeCalls = 0, fileWrites = 0, spawnCalls = 0;
  let serviceLog;
  try {
    fs.openSync = (file, ...args) => {
      const result = originals.openSync(file, ...args);
      if (file === logPath) descriptor = result;
      return result;
    };
    fs.chmodSync = (file, ...args) => {
      if (file === logPath && ["open-single", "open-dual", "detached-single", "detached-dual"].includes(scenario)) throw permissionError;
      return originals.chmodSync(file, ...args);
    };
    fs.closeSync = (file) => {
      originals.closeSync(file);
      if (file === descriptor) {
        closeCalls += 1;
        events.push("close");
        if (scenario.endsWith("-dual") || scenario.startsWith("spawn-close")) throw closeError;
      }
    };
    fs.writeSync = (file, buffer, offset, length, position) => {
      if (file !== descriptor) return originals.writeSync(file, buffer, offset, length, position);
      fileWrites += 1;
      if (scenario === "write-failure") throw writeError;
      const start = offset ?? 0;
      const available = length ?? buffer.byteLength - start;
      return originals.writeSync(file, buffer, start, Math.min(available, 2), position);
    };
    // 只执行真实描述符IO；创建及终止边界为替身，不启动后台进程。
    childProcess.spawn = (command, args, options) => {
      spawnCalls += 1;
      events.push("spawn");
      assert.equal(command, process.execPath);
      assert.deepEqual(args, []);
      assert.deepEqual(options, { cwd: fixtureRoot, env: {}, detached: true,
        stdio: ["ignore", descriptor, descriptor], windowsHide: true });
      if (["spawn-single", "spawn-dual"].includes(scenario)) throw spawnError;
      assert(scenario.startsWith("spawn-"), "初始化失败不能继续创建进程");
      return child;
    };
    process.kill = (pid, signal) => {
      assert.notEqual(platform, "win32");
      assert.equal(pid, -child.pid);
      assert.equal(signal, "SIGTERM");
      events.push("terminate");
      terminationCalls++;
      if (scenario === "spawn-close-cleanup") throw terminationError;
      return true;
    };
    childProcess.spawnSync = (command, args, options) => {
      assert.equal(platform, "win32");
      assert.equal(command, "taskkill");
      assert.deepEqual(args, ["/T", "/F", "/PID", String(child.pid)]);
      assert.equal(options.timeout, 5000);
      events.push("terminate");
      terminationCalls++;
      return scenario === "spawn-close-cleanup"
        ? { error: terminationError, status: null, stderr: "" }
        : { status: 0, stderr: "" };
    };
    syncBuiltinESMExports();
    const open = () => openServiceLog({ contract,
      boxteamHome: fixtureRoot, environment: { [SERVICE_LOG_PATH_ENV]: logPath },
      stdout: { write(value) { primaryWrites.push(value); return false; } },
      stderr: { write(value) { primaryWrites.push(value); return true; } },
    });
    if (scenario.startsWith("spawn-")) {
      let returnedChild, observedError;
      try {
        returnedChild = spawnDetachedProcess({ contract, command: process.execPath, args: [],
          cwd: fixtureRoot, environment: {}, logPath });
      } catch (error) { observedError = error; }
      assert.equal(spawnCalls, 1);
      assert.equal(closeCalls, 1);
      assert.throws(() => fs.fstatSync(descriptor), { code: "EBADF" });
      if (scenario === "spawn-success") {
        assert.equal(returnedChild, child);
        assert.equal(observedError, undefined);
      } else {
        assert.equal(returnedChild, undefined);
        if (scenario === "spawn-dual") {
          assert(observedError instanceof AggregateError);
          assert.deepEqual(observedError.errors, [spawnError, closeError]);
          assert.equal(observedError.cause, spawnError);
        } else if (scenario === "spawn-close-cleanup") {
          assert(observedError instanceof AggregateError);
          assert.equal(observedError.errors[0], closeError);
          assert.equal(observedError.errors.length, 2);
          const cleanupError = observedError.errors[1];
          assert.equal(platform === "win32" ? cleanupError.cause : cleanupError, terminationError);
          assert.equal(observedError.cause, closeError);
        } else {
          assert.equal(observedError, scenario === "spawn-single" ? spawnError : closeError);
        }
      }
      const cleanupExpected = scenario.startsWith("spawn-close") && scenario !== "spawn-close-no-pid";
      assert.equal(terminationCalls, cleanupExpected ? 1 : 0);
      assert.deepEqual(events, ["spawn", "close", ...(cleanupExpected ? ["terminate"] : [])]);
    } else if (scenario.startsWith("open-") || scenario.startsWith("detached-")) {
      let observedError;
      try {
        if (scenario.startsWith("detached-")) {
          spawnDetachedProcess({ contract, command: process.execPath, args: [], cwd: fixtureRoot, environment: {}, logPath });
        } else { serviceLog = open(); }
      } catch (error) { observedError = error; }
      if (scenario.endsWith("-dual")) {
        assert(observedError instanceof AggregateError, "初始化和清理失败都必须保留");
        assert.deepEqual(observedError.errors, [permissionError, closeError]);
        assert.equal(observedError.cause, permissionError);
      } else { assert.equal(observedError, permissionError); }
      assert.equal(spawnCalls, 0);
      assert.equal(closeCalls, 1);
      assert.throws(() => fs.fstatSync(descriptor), { code: "EBADF" });
    } else {
      serviceLog = open();
      if (scenario === "write-failure") {
        assert.throws(() => serviceLog.stdout.write("未落盘的日志"), (error) => error === writeError);
        assert.deepEqual(primaryWrites, []);
        assert.equal(fs.readFileSync(logPath).length, 0);
      } else {
        const stdout = Buffer.from("完整的stdout日志\n");
        const stderr = "完整的stderr日志\n";
        assert.equal(serviceLog.stdout.write(stdout), false);
        assert.equal(serviceLog.stderr.write(stderr), true);
        assert.equal(primaryWrites[0], stdout);
        assert.equal(primaryWrites[1], stderr);
        assert.deepEqual(fs.readFileSync(logPath), Buffer.concat([stdout, Buffer.from(stderr)]));
        assert(fileWrites > 2, "真实文件边界应被强制拆成多次短写");
      }
      serviceLog.close();
      serviceLog = undefined;
      assert.equal(closeCalls, 1);
      assert.throws(() => fs.fstatSync(descriptor), { code: "EBADF" });
    }
    return { scenario, platform, spawnCalls, closeCalls, terminationCalls, events };
  } finally {
    for (const [key, value] of Object.entries(originals)) fs[key] = value;
    childProcess.spawn = originalSpawn;
    childProcess.spawnSync = originalSpawnSync;
    process.kill = originalKill;
    Object.defineProperty(process, "platform", { value: originalPlatform });
    syncBuiltinESMExports();
    if (serviceLog) serviceLog.close();
    else if (descriptor !== undefined && closeCalls === 0) originals.closeSync(descriptor);
  }
}

function runServiceLogIo(scenario, platform = process.platform) {
  const fixtureRoot = path.join(workspaceRunRoot, platform, scenario);
  const artifactsRoot = path.join(artifactsRunRoot, platform, scenario);
  for (const directory of [fixtureRoot, artifactsRoot]) {
    mkdirSync(path.dirname(directory), { recursive: true });
    mkdirSync(directory);
  }
  const inputPath = path.join(artifactsRoot, "input.json");
  writeFileSync(inputPath, `${JSON.stringify([
    scenario, fixtureRoot,
    new URL("./service-log.mjs", import.meta.url).href,
    new URL("../detached-process.mjs", import.meta.url).href,
    new URL("../../../../src/shared/serverRuntimeLogging.mjs", import.meta.url).href,
    platform,
  ])}\n`, { flag: "wx" });
  const args = [observerPath, inputPath];
  const result = spawnSync("node", args, {
    cwd: fixtureRoot, timeout: 5000,
    env: { ...process.env, BOXTEAM_PROJECT_ROOT: process.cwd() },
  });
  const stdout = result.stdout ?? Buffer.alloc(0);
  const stderr = result.stderr ?? Buffer.alloc(0);
  writeFileSync(path.join(artifactsRoot, "stdout.raw"), stdout, { flag: "wx" });
  writeFileSync(path.join(artifactsRoot, "stderr.raw"), stderr, { flag: "wx" });
  writeFileSync(path.join(artifactsRoot, "result.json"), `${JSON.stringify({
    command: ["node", ...args], cwd: fixtureRoot, timeout_ms: 5000,
    status: result.status, signal: result.signal,
    error: result.error ? { name: result.error.name, message: result.error.message, code: result.error.code ?? null } : null,
  }, null, 2)}\n`, { flag: "wx" });
  if (result.error || result.status !== 0) {
    throw new Error(`服务日志 ${platform}/${scenario} 失败: exit=${result.status} signal=${result.signal}；artifacts=${artifactsRoot}\n${stderr.toString("utf8")}`, { cause: result.error });
  }
  return JSON.parse(stdout.toString("utf8"));
}

describe("Launcher服务日志文件IO与失败归属", () => {
  for (const [scenario, description] of [
    ["open-single", "前台日志初始化失败保留原异常并关闭描述符"],
    ["open-dual", "前台日志初始化和关闭同时失败保留两份原因"],
    ["detached-single", "后台日志初始化失败关闭描述符且不启动进程"],
    ["detached-dual", "后台日志初始化和关闭同时失败保留两份原因"],
  ]) {
    test.skipIf(process.platform === "win32")(description, () => runServiceLogIo(scenario));
  }
  test("短写仍完整保存两路UTF8日志并保留前台返回值", () => runServiceLogIo("partial-writes"));
  test("磁盘写入失败保留原异常且不伪造前台输出", () => runServiceLogIo("write-failure"));
});

describe("detached创建与日志描述符交付", () => {
  for (const platform of ["linux", "darwin", "win32"]) {
    for (const [scenario, description] of [
      ["spawn-success", "成功返回原child并关闭父端描述符"],
      ["spawn-single", "创建失败但关闭成功时保留原异常"],
      ["spawn-dual", "创建和关闭均失败时保留两份原异常"],
      ["spawn-close", "交付前关闭失败时终止未交付child"],
      ["spawn-close-no-pid", "无PID的创建句柄在关闭失败时不补造终止目标"],
      ["spawn-close-cleanup", "关闭及终止请求失败时保留两份原因"],
    ]) {
      test(`${platform}/${scenario} ${description}`, () => runServiceLogIo(scenario, platform));
    }
  }
});
