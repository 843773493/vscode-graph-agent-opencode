import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  parseLinuxProcStat,
  parsePosixPsStat,
  parsePosixSessionProcesses,
} from "../../../../shared/serverProcessLifecycle.mjs";

test("Linux stat 保留退出状态与身份，命令名中的空格和右括号不改变字段位置", () => {
  const fields = ["Z", "1", "4312", "4312", ...Array(15).fill("0"), "1000"];
  assert.deepEqual(
    parseLinuxProcStat(`4312 (worker ) name) ${fields.join(" ")}`),
    {
      pid: 4312,
      state: "Z",
      ppid: 1,
      processGroupId: 4312,
      processSessionId: 4312,
      processStartTime: 1000,
    },
  );
});

test("解析 BSD ps 的进程组、session 和启动身份", () => {
  assert.deepEqual(
    parsePosixPsStat(
      "  4312     1  4312  4312 Mon Jul 20 12:34:56 2026\n",
    ),
    {
      pid: 4312,
      ppid: 1,
      processGroupId: 4312,
      processSessionId: 4312,
      processStartTime: "Mon Jul 20 12:34:56 2026",
    },
  );
});

test("从 BSD ps 全量结果筛选同 session 进程", () => {
  const sessionId = 4312;
  assert.deepEqual(
    parsePosixSessionProcesses(
      [
        " 4312 4312",
        " 4315 4312",
        " 9000 9000",
      ].join("\n"),
      sessionId,
    ),
    [4315, 4312].filter((pid) => pid !== process.pid),
  );
});

test("Darwin sess 指针作为不透明 session 身份解析和筛选", () => {
  const sessionPointer = "10000a03";
  assert.equal(
    parsePosixPsStat(
      " 4312 1 4312 10000a03 Mon Jul 20 12:34:56 2026",
      { numericSession: false },
    ).processSessionId,
    sessionPointer,
  );
  assert.deepEqual(
    parsePosixSessionProcesses(
      " 4312 10000a03\n 4315 10000a03\n 9000 10000b00",
      sessionPointer,
    ),
    [4315, 4312].filter((pid) => pid !== process.pid),
  );
});

function observeNativeFailure(scenario, failSink = false) {
  const source = `
    import assert from "node:assert/strict";
    import fs from "node:fs/promises";
    import childProcess from "node:child_process";
    import { syncBuiltinESMExports } from "node:module";
    import { promisify } from "node:util";
    const [moduleUrl, scenario, failSink] = JSON.parse(process.argv[1]);
    const platform = scenario.startsWith("posix-") || scenario === "missing-posix" ? "darwin"
      : scenario.startsWith("taskkill-") || scenario === "windows-stat" ? "win32" : "linux";
    Object.defineProperty(process, "platform", { value: platform });
    const pid = 32109, privatePath = "/private/process-diagnostic";
    const code = scenario === "signal" ? "EPERM" : ["taskkill-exit", "taskkill-probe-after"].includes(scenario) ? 5
      : scenario === "taskkill-timeout" ? null : scenario === "taskkill-spawn" ? "ENOENT"
      : scenario === "missing-stat" || scenario === "missing-posix" || scenario === "missing-probe" ? "ESRCH"
      : scenario === "denied-probe" ? "EPERM" : "EACCES";
    const primary = Object.assign(new Error("原生操作失败: " + privatePath), {
      code, signal: scenario === "taskkill-timeout" ? "SIGTERM" : null,
      killed: scenario === "taskkill-timeout", stderr: privatePath,
    });
    const diagnostic = Object.assign(new Error("日志失败: /private/diagnostic-sink"), { code: "ENOTDIR" });
    const probeFailure = Object.assign(new Error("探测失败: /private/process-probe"), { code: "EIO" });
    const logs = [], signals = [];
    let taskkillCalls = 0;
    process.stderr.write = chunk => {
      logs.push(String(chunk));
      if (failSink) throw diagnostic;
      return true;
    };
    const read = fs.readFile;
    fs.readFile = async (file, ...args) => {
      if (typeof file !== "string" || !file.startsWith("/proc/")) return await read(file, ...args);
      const currentPid = Number(file.split("/")[2]);
      if (currentPid === pid && ["read", "missing-stat"].includes(scenario)) throw primary;
      if (currentPid === pid && scenario === "parse") return privatePath;
      const sid = currentPid === process.pid ? 40001 : pid;
      return currentPid + " (test) " + ["S", "1", sid, sid, ...Array(15).fill("0"), "1000"].join(" ");
    };
    fs.readdir = async directory => {
      assert.equal(directory, "/proc");
      if (scenario === "scan") throw primary;
      return [{ name: String(pid), isDirectory: () => true }];
    };
    process.kill = (target, signal) => {
      if (signal === 0) {
        if (scenario === "posix-probe-after" || (scenario === "taskkill-probe-after" && taskkillCalls > 0)) throw probeFailure;
        if (["probe", "missing-probe", "denied-probe", "missing-posix"].includes(scenario)) throw primary;
        return true;
      }
      signals.push([target, signal]);
      assert.equal(scenario, "signal", "不得向宿主发送任何真实信号");
      throw primary;
    };
    childProcess.execFile = (command, args, options, callback) => {
      if (command === "taskkill") taskkillCalls++;
      if (command === "taskkill" || scenario === "posix-read" || scenario === "posix-probe-after" || scenario === "missing-posix"
          || (scenario === "posix-scan" && args[0] === "-ax")) {
        callback(primary, "", primary.stderr);
      } else {
        assert.equal(command, "ps");
        const requested = Number(args.at(-1));
        const sid = requested === process.pid ? 40001 : pid;
        callback(null, requested + " 1 " + sid + " " + sid + " Mon Jul 20 12:34:56 2026", "");
      }
    };
    const exec = childProcess.execFile;
    exec[promisify.custom] = (...args) => new Promise((resolve, reject) => {
      exec(...args, (error, stdout, stderr) => error ? reject(error) : resolve({ stdout, stderr }));
    });
    syncBuiltinESMExports();
    const { readProcessStat, processExists, terminateProcessTree } = await import(moduleUrl);
    let failure, result;
    try {
      result = ["read", "parse", "posix-read", "posix-probe-after", "missing-stat", "missing-posix", "windows-stat"].includes(scenario)
        ? await readProcessStat(pid)
        : ["probe", "missing-probe", "denied-probe"].includes(scenario) ? processExists(pid)
        : await terminateProcessTree({ pid });
    } catch (error) { failure = error; }
    if (scenario.startsWith("missing-") || scenario === "windows-stat" || scenario === "denied-probe") {
      assert.equal(failure, undefined);
      assert.equal(result, scenario === "missing-probe" ? false : scenario === "denied-probe" ? true : null);
      assert.deepEqual(logs, []);
    } else {
      assert(failure);
      assert.match(failure.message, /^进程操作失败:/);
      assert(!failure.message.includes("/private/"));
      assert.equal(failure.code, scenario === "parse" ? undefined : primary.code);
      if (scenario.endsWith("probe-after")) {
        const combined = failSink ? failure.cause.errors[0] : failure.cause;
        assert(combined instanceof AggregateError);
        assert.equal(combined.cause, primary);
        assert.equal(combined.errors.length, 2);
        assert.equal(combined.errors[0], primary);
        const probe = combined.errors[1];
        assert.equal(probe.code, "EIO");
        assert(!probe.message.includes("/private/"));
        if (failSink) {
          assert.deepEqual(failure.cause.errors, [combined, diagnostic]);
          assert.equal(failure.cause.cause, combined);
          assert.deepEqual(probe.cause.errors, [probeFailure, diagnostic]);
          assert.equal(probe.cause.cause, probeFailure);
        } else assert.equal(probe.cause, probeFailure);
      } else if (failSink) {
        assert(failure.cause instanceof AggregateError);
        assert.deepEqual(failure.cause.errors, [scenario === "parse" ? failure.cause.errors[0] : primary, diagnostic]);
        assert.equal(failure.cause.cause, failure.cause.errors[0]);
      } else if (scenario !== "parse") assert.equal(failure.cause, primary);
      if (scenario === "parse") assert(failure.cause.cause?.message.includes(privatePath) || failure.cause.message.includes(privatePath));
      assert.equal(logs.length, scenario.endsWith("probe-after") ? 2 : 1);
      assert(logs.at(-1).includes(privatePath));
    }
    console.log(JSON.stringify({ signals, result, failed: Boolean(failure) }));
  `;
  return JSON.parse(execFileSync(process.execPath, ["--input-type=module", "--eval", source,
    JSON.stringify([new URL("../../../../shared/serverProcessLifecycle.mjs", import.meta.url).href, scenario, failSink])],
  { encoding: "utf8", timeout: 5000 }));
}

for (const scenario of ["read", "parse", "scan", "signal", "probe", "posix-read", "posix-scan", "taskkill-spawn", "taskkill-exit", "taskkill-timeout", "posix-probe-after", "taskkill-probe-after"]) {
  for (const failSink of [false, true]) {
    test(`原生${scenario}失败保留原因且日志${failSink ? "失败" : "正常"}不公开位置`, () => {
      const observed = observeNativeFailure(scenario, failSink);
      assert.equal(observed.failed, true);
      assert.deepEqual(observed.signals, scenario === "signal" ? [[32109, "SIGTERM"]] : []);
    });
  }
}

for (const scenario of ["missing-stat", "missing-posix", "missing-probe", "denied-probe", "windows-stat"]) {
  test(`原生${scenario}保持原缺失和存在性语义，不写失败日志`, () => {
    const observed = observeNativeFailure(scenario);
    assert.equal(observed.failed, false);
    assert.deepEqual(observed.signals, []);
  });
}

function observeForkDuringTermination(signal, explicitIdentity) {
  // 独立进程只替换 OS 边界；虚拟 PID 不会向宿主发送真实信号。
  const source = `
    import assert from "node:assert/strict";
    import fs from "node:fs/promises";
    import { syncBuiltinESMExports } from "node:module";
    const [moduleUrl, forkSignal, explicitIdentity] = JSON.parse(process.argv[1]);
    Object.defineProperty(process, "platform", { value: "linux" });
    const root = 41001, descendant = 41002;
    const states = new Map([[root, "S"]]);
    const signals = [];
    const originalReadFile = fs.readFile;
    fs.readFile = async (file, ...args) => {
      if (typeof file !== "string" || !file.startsWith("/proc/")) {
        return await originalReadFile(file, ...args);
      }
      const pid = Number(file.split("/")[2]);
      if (pid !== process.pid && !states.has(pid)) {
        throw Object.assign(new Error("进程已回收"), { code: "ENOENT" });
      }
      const sid = pid === process.pid ? 40001 : root;
      const fields = [states.get(pid) ?? "S", "1", sid, sid, ...Array(15).fill("0"), "1000"];
      return pid + " (test) " + fields.join(" ");
    };
    fs.readdir = async directory => {
      assert.equal(directory, "/proc");
      return [...states.keys()].map(pid => ({ name: String(pid), isDirectory: () => true }));
    };
    process.kill = (pid, signal) => {
      assert(states.has(pid));
      assert(["SIGTERM", "SIGKILL"].includes(signal));
      signals.push([pid, signal]);
      if (pid === root && signal === forkSignal) {
        // 表示快照完成到信号实际生效之间的 fork，不模拟 SIGKILL 处理器。
        states.delete(root);
        states.set(descendant, "S");
      } else if (signal === "SIGKILL") {
        states.set(pid, "Z");
      }
      return true;
    };
    syncBuiltinESMExports();
    const { terminateProcessTree } = await import(moduleUrl);
    const identity = explicitIdentity
      ? { pid: root, processSessionId: root, processStartTime: 1000 }
      : { pid: root };
    const result = await terminateProcessTree(identity);
    console.log(JSON.stringify({ result, signals, descendantState: states.get(descendant) }));
  `;
  const stdout = execFileSync(process.execPath, [
    "--input-type=module", "--eval", source,
    JSON.stringify([new URL("../../../../shared/serverProcessLifecycle.mjs", import.meta.url).href, signal, explicitIdentity]),
  ], { encoding: "utf8", timeout: 5000 });
  return JSON.parse(stdout);
}

for (const explicitIdentity of [true, false]) {
  test(`SIGTERM后根已回收时仍核实并终止新后代，显式身份=${explicitIdentity}`, () => {
    assert.deepEqual(observeForkDuringTermination("SIGTERM", explicitIdentity), {
      result: "force_killed",
      signals: [[41001, "SIGTERM"], [41002, "SIGKILL"]],
      descendantState: "Z",
    });
  });

  test(`SIGKILL快照后出现新后代时保留未停止结果，显式身份=${explicitIdentity}`, () => {
    assert.deepEqual(observeForkDuringTermination("SIGKILL", explicitIdentity), {
      result: "still_running",
      signals: [[41001, "SIGTERM"], [41001, "SIGKILL"]],
      descendantState: "S",
    });
  });
}
