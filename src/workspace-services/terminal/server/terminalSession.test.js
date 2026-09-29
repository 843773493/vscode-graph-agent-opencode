import assert from "node:assert/strict";
import test from "node:test";

import { TerminalSession } from "./terminalSession.js";

function completedSession() {
  const manager = {
    workspaceId: "gw_terminal_session_test",
    workspaceRoot: process.cwd(),
    attachUrl: (id) => `http://terminal.test/?terminalId=${id}`,
    async persist() {},
  };
  return new TerminalSession({
    manager,
    record: {
      terminal_id: "term_completed",
      workspace_id: manager.workspaceId,
      session_id: "session_owner",
      cwd_relative: ".",
      status: "running",
      last_command: "sleep 1",
      last_command_status: "completed",
      last_command_exit_code: 0,
      completion_event_id: "terminal_completed:term_completed:1",
    },
  });
}

test("只有返回给模型的后台 execution 可以 claim steering", () => {
  const session = completedSession();

  assert.equal(session.claimSteering(), false);
  session.markModelBackgrounded();
  assert.equal(session.claimSteering(), true);
  assert.equal(session.claimSteering(), false);
  session.finishSteering({ dispatched: true });
  assert.equal(session.snapshot().steering_dispatched, true);
});

test("模型读取完成输出后抑制 terminal completion steering", async () => {
  const session = completedSession();
  session.markModelBackgrounded();

  await session.readModelOutput();

  assert.equal(session.snapshot().completion_observed_by_model, true);
  assert.equal(session.claimSteering(), false);
});

test("持久记录只承载工作区内相对路径，绝不落盘任何真实绝对路径", () => {
  const session = completedSession();
  const record = session.toRecord();

  assert.equal(record.cwd_relative, ".");
  assert.equal("cwd" in record, false);
  // 负向断言（6.3）：持久记录序列化结果不得出现工作区绝对路径。
  assert.equal(JSON.stringify(record).includes(process.cwd()), false);
});

test("restore 用工作区根 + 相对路径在调用栈内重推导 PTY cwd", () => {
  const manager = {
    workspaceId: "gw_terminal_session_test",
    workspaceRoot: process.cwd(),
    attachUrl: (id) => `http://terminal.test/?terminalId=${id}`,
    async persist() {},
  };
  const session = new TerminalSession({
    manager,
    record: {
      terminal_id: "term_relative",
      workspace_id: manager.workspaceId,
      session_id: "session_owner",
      cwd_relative: "src",
      status: "exited",
    },
  });

  assert.equal(session.cwdRelative, "src");
  assert.equal(session.cwd, `${process.cwd()}/src`);
  assert.equal(session.toRecord().cwd_relative, "src");
});

test("持久记录缺少工作目录相对路径时显式失败而不是静默回退", () => {
  const manager = {
    workspaceId: "gw_terminal_session_test",
    workspaceRoot: process.cwd(),
    attachUrl: (id) => `http://terminal.test/?terminalId=${id}`,
    async persist() {},
  };

  assert.throws(
    () => new TerminalSession({
      manager,
      record: {
        terminal_id: "term_missing_cwd",
        workspace_id: manager.workspaceId,
        session_id: "session_owner",
        status: "exited",
      },
    }),
    /终端记录缺少工作目录相对路径/,
  );
});
