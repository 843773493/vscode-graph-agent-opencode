import assert from "node:assert/strict";
import test from "node:test";

import { TerminalSession } from "./terminalSession.js";

function completedSession() {
  const manager = {
    workspaceId: "gw_terminal_session_test",
    attachUrl: (id) => `http://terminal.test/?terminalId=${id}`,
    async persist() {},
  };
  return new TerminalSession({
    manager,
    record: {
      terminal_id: "term_completed",
      workspace_id: manager.workspaceId,
      session_id: "session_owner",
      cwd: process.cwd(),
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
