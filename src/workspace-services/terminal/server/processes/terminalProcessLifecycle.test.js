import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  parseLinuxProcStat,
  parsePosixPsStat,
  parsePosixSessionProcesses,
} from "./terminalProcessLifecycle.js";

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
    const { terminateTerminalProcessTree } = await import(moduleUrl);
    const identity = explicitIdentity
      ? { pid: root, processSessionId: root, processStartTime: 1000 }
      : { pid: root };
    const result = await terminateTerminalProcessTree(identity);
    console.log(JSON.stringify({ result, signals, descendantState: states.get(descendant) }));
  `;
  const stdout = execFileSync(process.execPath, [
    "--input-type=module", "--eval", source,
    JSON.stringify([new URL("./terminalProcessLifecycle.js", import.meta.url).href, signal, explicitIdentity]),
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
