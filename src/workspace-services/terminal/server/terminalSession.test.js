import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { mkdirSync } from "node:fs";

import { TerminalSession, validateTerminalRecord } from "./terminalSession.js";

const workspaceRoot = path.join(process.cwd(), "out", "tests", "src", "workspace-services", "terminal", "server", "terminalSession", "workspace");
mkdirSync(workspaceRoot, { recursive: true });

function completedSession(overrides = {}) {
  const manager = {
    workspaceId: "00000000-0000-4000-8000-000000000001",
    workspaceRoot,
    async persist() {},
  };
  return new TerminalSession({
    manager,
    record: {
      terminal_id: "term_completed",
      workspace_id: manager.workspaceId,
      transport_owner_revision: 1,
      reconciliation_status: "verified",
      cwd_relative: ".",
      status: "running",
      last_command: "sleep 1",
      last_command_status: "completed",
      last_command_exit_code: 0,
      completion_event_id: "terminal_completed:term_completed:1",
      command_start_sequence: 0,
      ...overrides,
    },
  });
}

test("只有返回给模型的后台 execution 可以 claim steering", () => {
  const session = completedSession();
  const identity = { completionEventId: session.completionEventId, commandStartSequence: session.commandStartSequence };

  assert.equal(session.claimSteering(identity), false);
  session.markModelBackgrounded();
  assert.equal(session.claimSteering(identity), true);
  assert.equal(session.claimSteering(identity), true);
  session.finishSteering({ ...identity, dispatched: true });
  session.finishSteering({ ...identity, dispatched: true });
  assert.equal(session.claimSteering(identity), false);
  assert.equal(session.snapshot().steering_dispatched, true);
});

test("迟到 claim 和 finish 不得修改另一命令 generation", () => {
  const session = completedSession({ steering_dispatching: true });
  const current = { completionEventId: session.completionEventId, commandStartSequence: session.commandStartSequence };
  const before = session.toRecord();

  for (const stale of [
    { ...current, completionEventId: "terminal_completed:term_completed:old" },
    { ...current, commandStartSequence: 2 },
  ]) {
    assert.throws(() => session.claimSteering(stale), /event\/generation 不匹配/);
    assert.throws(() => session.finishSteering({ ...stale, dispatched: true }), /event\/generation 不匹配/);
  }
  assert.deepEqual(session.toRecord(), before);
});

test("恢复持久 claim 保留原 completion event 和 generation", () => {
  const session = completedSession({ steering_dispatching: true });
  const identity = { completionEventId: session.completionEventId, commandStartSequence: session.commandStartSequence };
  assert.equal(session.claimSteering(identity), true);
  session.finishSteering({ ...identity, dispatched: true });
  assert.equal(session.snapshot().steering_dispatched, true);
});

test("模型读取完成输出后抑制 terminal completion steering", async () => {
  const session = completedSession();
  session.markModelBackgrounded();

  await session.readModelOutput();

  assert.equal(session.snapshot().completion_observed_by_model, true);
  assert.equal(session.claimSteering({ completionEventId: session.completionEventId, commandStartSequence: session.commandStartSequence }), false);
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
    workspaceId: "00000000-0000-4000-8000-000000000001",
    workspaceRoot,
    async persist() {},
  };
  const session = new TerminalSession({
    manager,
    record: {
      terminal_id: "term_relative",
      workspace_id: manager.workspaceId,
      transport_owner_revision: 1,
      reconciliation_status: "verified",
      cwd_relative: "src",
      status: "exited",
    },
  });

  assert.equal(session.cwdRelative, "src");
  assert.equal(session.cwd, path.join(workspaceRoot, "src"));
  assert.equal(session.toRecord().cwd_relative, "src");
});

test("持久记录缺少工作目录相对路径时显式失败而不是静默回退", () => {
  const manager = {
    workspaceId: "00000000-0000-4000-8000-000000000001",
    workspaceRoot,
    async persist() {},
  };

  assert.throws(
    () => new TerminalSession({
      manager,
      record: {
        terminal_id: "term_missing_cwd",
        workspace_id: manager.workspaceId,
        transport_owner_revision: 1,
        reconciliation_status: "verified",
        status: "exited",
      },
    }),
    /必须包含相对 cwd_relative/,
  );
});

test("持久记录必须由当前工作区 owner 创建", () => {
  const manager = {
    workspaceId: "00000000-0000-4000-8000-000000000001",
    workspaceRoot,
    async persist() {},
  };

  assert.throws(
    () => new TerminalSession({
      manager,
      record: {
        terminal_id: "term_wrong_owner",
        workspace_id: "another_workspace",
        cwd_relative: ".",
        status: "exited",
      },
    }),
    /终端记录工作区身份不匹配.*term_wrong_owner/,
  );
});

test("Node 只保存传输版本并拒绝领域 owner 字段", () => {
  const record = completedSession().toRecord();
  assert.equal(record.transport_owner_revision, 1);
  assert.equal(record.reconciliation_status, "verified");
  assert.equal("attach_url" in record, false);
  for (const field of ["session_id", "source_thread_id", "retention_scope", "thread_associations", "unattached_since", "owner_revision"]) {
    assert.equal(field in record, false);
    assert.throws(() => validateTerminalRecord({ ...record, [field]: null }, record.workspace_id), /不得持有领域 owner 字段/);
  }
});

test("提升传输版本立即撤销旧连接而不终止 PTY", () => {
  const session = completedSession();
  const closed = [];
  const client = { close: (code, reason) => closed.push({ code, reason }) };
  session.transports.set(client, { revision: 1 });
  session.assertTransport(client);
  session.syncOwnerRevision(2);
  assert.deepEqual(closed, [{ code: 4009, reason: "terminal owner changed" }]);
  assert.throws(() => session.assertTransport(client), /连接授权已过期/);
  assert.equal(session.snapshot().status, "running");
  assert.throws(() => session.syncOwnerRevision(1), /过期或无效/);
});


test("owner 待核实时拒绝 resize 且保留原尺寸", () => {
  const session = completedSession();
  session.markReconciliationStatus("unknown");

  assert.throws(() => session.resize(120, 40), /尚待 owner 核实/);
  assert.equal(session.snapshot().cols, 100);
  assert.equal(session.snapshot().rows, 30);
});

test("attach 持久失败不留下传输授权或客户端句柄", async () => {
  const session = completedSession();
  let calls = 0;
  session.manager.persist = async () => {
    if (calls++ === 0) throw new Error("attach 写盘失败");
  };
  const sent = [];
  const client = { sendJson: (message) => { sent.push(message); return true; }, close() {} };
  await assert.rejects(session.attach(client, { grant: { revision: 1 } }), /写盘失败/);
  assert.equal(session.transports.size, 0);
  assert.equal(session.outputMultiplexer.clientCount, 0);
  assert.equal(sent.some((message) => message.type === "attached"), false);
  assert.throws(() => session.assertTransport(client), /授权已过期/);
});

test("attach await 写盘期间版本撤销先于 attached 回执，失败后全部回收", { timeout: 5000 }, async () => {
  const session = completedSession();
  const entered = Promise.withResolvers();
  const resume = Promise.withResolvers();
  session.manager.persist = async () => { entered.resolve(); await resume.promise; };
  const sent = [];
  const closed = [];
  const client = {
    sendJson: (message) => { sent.push(message); return true; },
    close: (code, reason) => closed.push({ code, reason }),
  };
  const attaching = session.attach(client, { grant: { revision: 1 } });
  const rejection = assert.rejects(attaching, /授权已过期/);
  try {
    await entered.promise;
    session.syncOwnerRevision(2);
  } finally {
    resume.resolve();
    await rejection;
  }
  assert.deepEqual(closed, [{ code: 4009, reason: "terminal owner changed" }]);
  assert.equal(session.transports.size, 0);
  assert.equal(session.outputMultiplexer.clientCount, 0);
  assert.equal(sent.some((message) => message.type === "attached"), false);
  assert.equal(session.status, "running");
});
