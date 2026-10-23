import assert from "node:assert/strict";
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
