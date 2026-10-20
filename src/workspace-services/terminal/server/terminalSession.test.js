import assert from "node:assert/strict";
import test, { after } from "node:test";
import path from "node:path";
import { cpSync, rmSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

import { TerminalSession, validateTerminalRecord } from "./terminalSession.js";
import { IsolatedPtyProcess } from "./isolatedPtyProcess.js";

const workspaceRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalSession/workspace", randomUUID());
// 文件级清理兜住准备失败；用例结束优先释放本次创建的独占工作区。
after(() => rmSync(workspaceRunRoot, { recursive: true, force: true }));

function createTerminalSessionTestWorkspace(t) {
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  t.after(() => rmSync(workspaceRoot, { recursive: true, force: true }));
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/default_test_workspace"), workspaceRoot, { recursive: true });
  writeFileSync(path.join(workspaceRoot, ".boxteam/workspace-identity.json"), JSON.stringify({
    workspace_id: "00000000-0000-4000-8000-000000000001",
  }));
  return workspaceRoot;
}

function completedSession(t, overrides = {}) {
  const workspaceRoot = createTerminalSessionTestWorkspace(t);
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

test("只有返回给模型的后台 execution 可以 claim steering", (t) => {
  const session = completedSession(t);
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

test("迟到 claim 和 finish 不得修改另一命令 generation", (t) => {
  const session = completedSession(t, { steering_dispatching: true });
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

test("恢复持久 claim 保留原 completion event 和 generation", (t) => {
  const session = completedSession(t, { steering_dispatching: true });
  const identity = { completionEventId: session.completionEventId, commandStartSequence: session.commandStartSequence };
  assert.equal(session.claimSteering(identity), true);
  session.finishSteering({ ...identity, dispatched: true });
  assert.equal(session.snapshot().steering_dispatched, true);
});

test("模型读取完成输出后抑制 terminal completion steering", async (t) => {
  const session = completedSession(t);
  session.markModelBackgrounded();

  await session.readModelOutput();

  assert.equal(session.snapshot().completion_observed_by_model, true);
  assert.equal(session.claimSteering({ completionEventId: session.completionEventId, commandStartSequence: session.commandStartSequence }), false);
});

test("持久记录只承载工作区内相对路径，绝不落盘任何真实绝对路径", (t) => {
  const session = completedSession(t);
  const record = session.toRecord();

  assert.equal(record.cwd_relative, ".");
  assert.equal("cwd" in record, false);
  // 负向断言（6.3）：持久记录序列化结果不得出现工作区绝对路径。
  assert.equal(JSON.stringify(record).includes(process.cwd()), false);
});

test("restore 用工作区根 + 相对路径在调用栈内重推导 PTY cwd", async (t) => {
  const workspaceRoot = createTerminalSessionTestWorkspace(t);
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
  let workerCwd;
  t.mock.method(IsolatedPtyProcess.prototype, "start", async ({ options }) => {
    workerCwd = options.cwd;
    return { pid: null };
  });
  t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  try {
    await session.start();
    assert.equal(workerCwd, path.join(workspaceRoot, "src"));
  } finally {
    await session.dispose();
  }
});

test("持久记录缺少工作目录相对路径时显式失败而不是静默回退", (t) => {
  const workspaceRoot = createTerminalSessionTestWorkspace(t);
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

test("持久记录必须由当前工作区 owner 创建", (t) => {
  const workspaceRoot = createTerminalSessionTestWorkspace(t);
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

test("Node 只保存传输版本并拒绝领域 owner 字段", (t) => {
  const record = completedSession(t).toRecord();
  assert.equal(record.transport_owner_revision, 1);
  assert.equal(record.reconciliation_status, "verified");
  assert.equal("attach_url" in record, false);
  for (const field of ["session_id", "source_thread_id", "retention_scope", "thread_associations", "unattached_since", "owner_revision"]) {
    assert.equal(field in record, false);
    assert.throws(() => validateTerminalRecord({ ...record, [field]: null }, record.workspace_id), /不得持有领域 owner 字段/);
  }
});

test("提升传输版本立即撤销旧连接而不终止 PTY", (t) => {
  const session = completedSession(t);
  const closed = [];
  const client = { close: (code, reason) => closed.push({ code, reason }) };
  session.transports.set(client, { transportOwnerRevision: 1 });
  session.assertTransport(client);
  session.syncOwnerRevision(2);
  assert.deepEqual(closed, [{ code: 4009, reason: "terminal owner changed" }]);
  assert.throws(() => session.assertTransport(client), /连接授权已过期/);
  assert.equal(session.snapshot().status, "running");
  assert.throws(() => session.syncOwnerRevision(1), /过期或无效/);
});


test("owner 待核实时拒绝 resize 且保留原尺寸", (t) => {
  const session = completedSession(t);
  session.markReconciliationStatus("unknown");

  assert.throws(() => session.resize(120, 40), /尚待 owner 核实/);
  assert.equal(session.snapshot().cols, 100);
  assert.equal(session.snapshot().rows, 30);
});

test("attach 持久失败不留下传输授权或客户端句柄", async (t) => {
  const session = completedSession(t);
  let calls = 0;
  session.manager.persist = async () => {
    if (calls++ === 0) throw new Error("attach 写盘失败");
  };
  const sent = [];
  const client = { sendJson: (message) => { sent.push(message); return true; }, close() {} };
  await assert.rejects(session.attach(client, { grant: { transportOwnerRevision: 1 } }), /写盘失败/);
  assert.equal(session.transports.size, 0);
  assert.equal(session.outputMultiplexer.clientCount, 0);
  assert.equal(sent.some((message) => message.type === "attached"), false);
  assert.throws(() => session.assertTransport(client), /授权已过期/);
});

test("attach await 写盘期间版本撤销先于 attached 回执，失败后全部回收", { timeout: 5000 }, async (t) => {
  const session = completedSession(t);
  const entered = Promise.withResolvers();
  const resume = Promise.withResolvers();
  session.manager.persist = async () => { entered.resolve(); await resume.promise; };
  const sent = [];
  const closed = [];
  const client = {
    sendJson: (message) => { sent.push(message); return true; },
    close: (code, reason) => closed.push({ code, reason }),
  };
  const attaching = session.attach(client, { grant: { transportOwnerRevision: 1 } });
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

test("多行粘贴仅以最后有效行更新输入标签，PTY仍收到完整原文", async (t) => {
  const writes = [];
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => ({ pid: null }));
  t.mock.method(IsolatedPtyProcess.prototype, "write", (data) => writes.push(data));
  t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  const session = completedSession(t, { last_command: null, last_command_status: null });
  await session.start();
  try {
    const paste = `${"  旧行中🙂  \r\n".repeat(50000)}  echo 新正文🙂  \r\n\t\u00a0`;
    session.write(paste, { source: "user" });
    const first = session.toRecord();
    assert.equal(first.last_input, "echo 新正文🙂");
    assert.equal(first.last_input_source, "user");
    assert(Number.isFinite(Date.parse(first.last_input_at)));

    const whitespace = " \t\v\r\n\u00a0\u2028\ufeff";
    session.write(whitespace, { source: "agent" });
    const second = session.toRecord();
    assert.equal(second.last_input, first.last_input);
    assert.equal(second.last_input_source, first.last_input_source);
    assert.equal(second.last_input_at, first.last_input_at);
    assert.deepEqual(writes, [paste, whitespace]);
    assert.equal(second.last_command, null);
    assert.equal(second.last_command_status, null);
  } finally {
    await session.dispose();
  }
});

test("没有运行中的模型命令时，PTY输出保留正文与序号但不扫描历史标记", async (t) => {
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => ({ pid: null }));
  t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  const exec = RegExp.prototype.exec;
  let scans = 0;
  t.mock.method(RegExp.prototype, "exec", function (input) {
    if (this.source.includes("__BOXTEAM_CMD_DONE_") && this.global && this.lastIndex === 0) scans += 1;
    return exec.call(this, input);
  });
  for (const commandStatus of [null, "completed", "exited", "terminated"]) {
    const session = completedSession(t, { last_command: null, last_command_status: commandStatus });
    await session.start();
    try {
      const output = "\x1b[31m交互正文 中😀\x1b[0m\n__BOXTEAM_CMD_DONE_deadbeef__:9\n";
      for (const receive of session.ptyProcess.ptyOutputHandlers) receive(output);
      assert.equal(session.buffer, output);
      assert.equal(session.outputMultiplexer.readAfter(0).output, output);
      assert.equal(session.sequence, 1);
      assert.equal(session.lastCommandStatus, commandStatus);
      assert.equal(session.status, "running");
      assert.equal(scans, 0);
    } finally {
      await session.dispose();
    }
  }
});

test("模型命令仍解析跨块和ANSI标记，完成后停止扫描且只释放一次", async (t) => {
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => ({ pid: null }));
  t.mock.method(IsolatedPtyProcess.prototype, "write", () => {});
  t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  const session = completedSession(t, { last_command: null, last_command_status: null, completion_event_id: null });
  await session.start();
  const released = [];
  t.mock.method(session, "terminateForRelease", async (options) => { released.push(options); return true; });
  const exec = RegExp.prototype.exec;
  let scans = 0;
  t.mock.method(RegExp.prototype, "exec", function (input) {
    if (this.source.includes("__BOXTEAM_CMD_DONE_") && this.global && this.lastIndex === 0) scans += 1;
    return exec.call(this, input);
  });
  try {
    session.write("__BOXTEAM_CMD_START_cafe__\n__BOXTEAM_CMD_DONE_cafe__", { source: "agent", command: "exit 7" });
    const outputs = ["旧标记 __BOXTEAM_CMD_DONE_deadbeef__:9\n", "\x1b[31m__BOXTEAM_CMD_DONE_ca", "fe__\x1b[0m:7\r\n", "尾部正文 __BOXTEAM_CMD_DONE_cafe__:8\n"];
    for (const [index, output] of outputs.entries()) {
      for (const receive of session.ptyProcess.ptyOutputHandlers) receive(output);
      if (index < 2) assert.equal(session.lastCommandStatus, "running");
    }
    assert.equal(scans, 3);
    assert.equal(session.lastCommandStatus, "completed");
    assert.equal(session.lastCommandExitCode, 7);
    assert.equal(session.sequence, 4);
    assert.equal(session.commandStartSequence, 0);
    assert.equal(session.completionEventId, `terminal_completed:${session.id}:3`);
    assert.deepEqual(released, [{ status: "completed", commandStatus: "completed", reason: "command_completed" }]);
    assert.equal(session.outputMultiplexer.readAfter(0).output, outputs.join(""));
  } finally {
    await session.dispose();
  }
});
