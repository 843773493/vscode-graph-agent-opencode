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
  const originals = Object.fromEntries(["openSync", "chmodSync", "closeSync", "writeSync", "writeFileSync", "unlinkSync"].map((key) => [key, fs[key]]));
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
  const guardCloseError = new Error("injected guard close failure");
  const recordWriteError = new Error("injected ownership write failure");
  const recordCloseError = new Error("injected ownership close failure");
  const recordRemoveError = new Error("injected ownership remove failure");
  const child = scenario === "spawn-close-no-pid" ? {} : { pid: 32109 };
  const events = [];
  let terminationCalls = 0;
  const primaryWrites = [];
  let descriptor, closeCalls = 0, fileWrites = 0, spawnCalls = 0;
  let guardDescriptor, recordDescriptor, recordPath;
  let guardCloseCalls = 0, recordCloseCalls = 0, recordRemoveCalls = 0;
  let serviceLog;
  try {
    fs.openSync = (file, ...args) => {
      const result = originals.openSync(file, ...args);
      if (file === logPath && descriptor === undefined) descriptor = result;
      if (file === `${logPath}.owner.lock`) guardDescriptor = result;
      if (typeof file === "string" && file.startsWith(`${logPath}.owner.json.`) && file.endsWith(".tmp")) {
        recordDescriptor = result;
        recordPath = file;
      }
      return result;
    };
    fs.chmodSync = (file, ...args) => {
      if (file === logPath && ["open-single", "open-dual", "detached-single", "detached-dual", "guard-open-dual"].includes(scenario)) throw permissionError;
      return originals.chmodSync(file, ...args);
    };
    fs.closeSync = (file) => {
      originals.closeSync(file);
      if (file === guardDescriptor) {
        guardCloseCalls += 1;
        if (scenario.startsWith("guard-")) throw guardCloseError;
      } else if (file === recordDescriptor) {
        recordCloseCalls += 1;
        if (["record-close", "record-write-close-remove"].includes(scenario)) throw recordCloseError;
      } else if (file === descriptor) {
        closeCalls += 1;
        events.push("close");
        if (["open-dual", "detached-dual", "spawn-dual", "guard-close-dual"].includes(scenario) || scenario.startsWith("spawn-close")) throw closeError;
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
    fs.writeFileSync = (file, ...args) => {
      if (file === recordDescriptor && scenario.startsWith("record-write")) throw recordWriteError;
      return originals.writeFileSync(file, ...args);
    };
    fs.unlinkSync = (file) => {
      if (file === recordPath) {
        recordRemoveCalls += 1;
        if (scenario === "record-write-close-remove") throw recordRemoveError;
      }
      return originals.unlinkSync(file);
    };
    // 只执行真实描述符IO；创建及终止边界为替身，不启动后台进程。
    childProcess.spawn = (command, args, options) => {
      spawnCalls += 1;
      events.push("spawn");
      assert.equal(command, process.execPath);
      assert.deepEqual(args, []);
      const sink = JSON.parse(options.env.BOXTEAM_RUNTIME_LOG_SINK);
      assert.equal(sink.path, logPath);
      assert.equal(sink.role, "launcher");
      assert.equal(options.env.BOXTEAM_SERVICE_LOG_CAPTURED, "1");
      assert.deepEqual(options, { cwd: fixtureRoot, env: {
        BOXTEAM_RUNTIME_LOG_SINK: JSON.stringify(sink), BOXTEAM_SERVICE_LOG_CAPTURED: "1",
      }, detached: true,
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
    if (scenario.startsWith("guard-") || scenario.startsWith("record-")) {
      let observedError;
      try { serviceLog = await open(); }
      catch (error) { observedError = error; }
      assert.equal(serviceLog, undefined, "清理失败不能交付未完成的文件 owner");
      const expected = {
        "guard-close": [guardCloseError],
        "guard-close-dual": [guardCloseError, closeError],
        "guard-open-dual": [permissionError, guardCloseError],
        "record-write": [recordWriteError],
        "record-close": [recordCloseError],
        "record-write-close-remove": [recordWriteError, recordCloseError, recordRemoveError],
      }[scenario];
      if (expected.length === 1) assert.equal(observedError, expected[0]);
      else {
        assert(observedError instanceof AggregateError);
        assert.deepEqual(observedError.errors, expected);
        assert.equal(observedError.cause, expected[0]);
      }
      assert.equal(closeCalls, 1);
      assert.equal(guardCloseCalls, 1);
      assert.equal(spawnCalls, 0);
      assert.throws(() => fs.fstatSync(descriptor), { code: "EBADF" });
      assert.throws(() => fs.fstatSync(guardDescriptor), { code: "EBADF" });
      if (scenario.startsWith("record-")) {
        assert.equal(recordCloseCalls, 1);
        assert.equal(recordRemoveCalls, 1);
        assert.throws(() => fs.fstatSync(recordDescriptor), { code: "EBADF" });
        assert.equal(fs.existsSync(`${logPath}.owner.json`), false, "失败的归属正文不能发布");
        assert.equal(fs.existsSync(recordPath), scenario === "record-write-close-remove");
      } else if (scenario !== "guard-open-dual") {
        // guard 失败已释放本次句柄，后续 owner 应能重新取得同一路径。
        for (const [key, value] of Object.entries(originals)) fs[key] = value;
        childProcess.spawnSync = originalSpawnSync;
        syncBuiltinESMExports();
        const reopened = await open();
        reopened.close();
      }
    } else if (scenario.startsWith("spawn-")) {
      let returnedChild, observedError;
      try {
        returnedChild = await spawnDetachedProcess({ contract, command: process.execPath, args: [],
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
          await spawnDetachedProcess({ contract, command: process.execPath, args: [], cwd: fixtureRoot, environment: {}, logPath });
        } else { serviceLog = await open(); }
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
      serviceLog = await open();
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
  // 场景文件均使用绝对路径；避免 Windows CreateProcess 因深层 cwd 而拒绝启动观察器。
  const observerCwd = process.cwd();
  const result = spawnSync("node", args, {
    cwd: observerCwd, timeout: 5000,
    env: { ...process.env, BOXTEAM_PROJECT_ROOT: process.cwd() },
  });
  const stdout = result.stdout ?? Buffer.alloc(0);
  const stderr = result.stderr ?? Buffer.alloc(0);
  writeFileSync(path.join(artifactsRoot, "stdout.raw"), stdout, { flag: "wx" });
  writeFileSync(path.join(artifactsRoot, "stderr.raw"), stderr, { flag: "wx" });
  writeFileSync(path.join(artifactsRoot, "result.json"), `${JSON.stringify({
    command: ["node", ...args], cwd: observerCwd, timeout_ms: 5000,
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
  for (const [scenario, description] of [
    ["guard-close", "所有权锁关闭失败时释放未交付的日志句柄"],
    ["guard-close-dual", "所有权锁与日志关闭失败保留两份原异常"],
    ["record-write", "归属正文写入失败时不发布且释放全部句柄"],
    ["record-close", "归属记录关闭失败时不发布且清理临时文件"],
    ["record-write-close-remove", "归属写入关闭和清理失败保留原异常顺序"],
  ]) {
    test(description, () => runServiceLogIo(scenario));
  }
  test.skipIf(process.platform === "win32")("初始化和所有权锁关闭失败保留原异常顺序", () => runServiceLogIo("guard-open-dual"));
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
