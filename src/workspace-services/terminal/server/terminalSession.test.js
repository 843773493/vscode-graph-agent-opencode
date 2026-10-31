import { inspect } from "node:util";
import assert from "node:assert/strict";
import test, { after } from "node:test";
import path from "node:path";
import { cpSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

import { resolveTerminalCwd, TerminalSession, validateTerminalRecord } from "./terminalSession.js";
import { IsolatedPtyProcess } from "./processes/isolatedPtyProcess.js";
import { readProcessStat } from "./processes/terminalProcessLifecycle.js";

const workspaceRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalSession/workspace", randomUUID());
// 文件级清理兜住准备失败；用例结束优先释放本次创建的独占工作区。
after(() => rmSync(workspaceRunRoot, { recursive: true, force: true }));

function createTerminalSessionTestWorkspace(t) {
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  t.after(() => rmSync(workspaceRoot, { recursive: true, force: true }));
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/terminal_cwd_workspace"), workspaceRoot, { recursive: true });
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

test("PTY 启动失败只公开终端身份，保留底层 cause 和原生启动诊断", async (t) => {
  const session = completedSession(t, { status: "created" });
  const original = new Error(`spawn ${session.cwd}/private-shell EACCES`);
  const diagnostic = t.mock.method(process.stderr, "write", () => {});
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => { throw original; });
  try {
    await assert.rejects(session.start(), (error) => {
      assert.equal(error.message, `终端 PTY 启动失败: terminal_id=${session.id}`);
      assert.equal(error.cause, original);
      return true;
    });
    assert.equal(session.status, "created");
    const [message] = diagnostic.mock.calls[0].arguments;
    assert(message.includes(`[terminal-session] 终端 PTY 启动失败: terminal_id=${session.id}`));
    assert(message.includes(inspect({ cwd: session.cwd, command: session.command })));
    assert(message.includes(inspect(original)));
  } finally {
    await session.dispose();
  }
});

for (const cwdKind of ["relative-missing", "absolute-missing", "file", "nul"]) {
  test(`启动诊断次错不覆盖${cwdKind}工作目录拒绝`, async (t) => {
    const root = createTerminalSessionTestWorkspace(t);
    const file = path.join(root, "not-a-directory.txt");
    writeFileSync(file, "原件不变");
    const cwd = cwdKind === "relative-missing" ? "missing"
      : cwdKind === "absolute-missing" ? path.join(root, "absolute-missing")
        : cwdKind === "file" ? file : "bad\0directory";
    const message = cwdKind === "nul" ? "终端工作目录检查失败" : "终端工作目录不存在或不是目录";
    const diagnosticError = new Error(`日志写入失败: ${root}/private-log`);
    const diagnostic = t.mock.method(process.stderr, "write", () => { throw diagnosticError; });
    await assert.rejects(resolveTerminalCwd(root, cwd), (error) => {
      assert.equal(error.message, message);
      const aggregate = cwdKind === "file" ? error : error.cause;
      assert(aggregate instanceof AggregateError);
      const [original, secondary] = aggregate.errors;
      assert.equal(aggregate.errors.length, 2);
      assert.equal(aggregate.cause, original);
      assert.equal(secondary, diagnosticError);
      if (cwdKind === "file") {
        assert.equal(original.message, message);
        assert.equal(original.cause, undefined, "不得形成自引用cause");
      } else {
        assert.equal(original.code, cwdKind === "nul" ? "ERR_INVALID_ARG_VALUE" : "ENOENT");
        assert(diagnostic.mock.calls[0].arguments[0].includes(inspect(original)));
      }
      assert(diagnostic.mock.calls[0].arguments[0].includes(inspect({ cwd: path.resolve(root, cwd) })));
      return true;
    });
    assert.equal(diagnostic.mock.callCount(), 1);
    t.mock.restoreAll();
    assert.equal(await resolveTerminalCwd(root, "."), root);
  });
}

test("启动诊断次错保留原PTY异常和原Worker句柄", async (t) => {
  const session = completedSession(t, { status: "created" });
  const original = new Error(`spawn ${session.cwd}/private-shell EACCES`);
  const diagnosticError = new Error(`日志写入失败: ${session.cwd}/private-log`);
  const diagnostic = t.mock.method(process.stderr, "write", () => { throw diagnosticError; });
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => { throw original; });
  try {
    await assert.rejects(session.start(), (error) => {
      assert.equal(error.message, `终端 PTY 启动失败: terminal_id=${session.id}`);
      assert(error.cause instanceof AggregateError);
      assert.deepEqual(error.cause.errors, [original, diagnosticError]);
      assert.equal(error.cause.cause, original);
      return true;
    });
    assert(session.ptyProcess instanceof IsolatedPtyProcess);
    assert.equal(session.status, "created");
    assert.equal(session.osPid, null);
    assert.equal(diagnostic.mock.callCount(), 1);
    assert(diagnostic.mock.calls[0].arguments[0].includes(inspect(original)));
  } finally {
    await session.dispose();
  }
});

test("启动诊断次错逐层保留cwd及PTY阶段原因", async (t) => {
  const session = completedSession(t, { status: "created", cwd_relative: "missing" });
  const diagnosticError = new Error(`日志写入失败: ${session.cwd}/private-log`);
  const diagnostic = t.mock.method(process.stderr, "write", () => { throw diagnosticError; });
  const start = t.mock.method(IsolatedPtyProcess.prototype, "start", async () => assert.fail("cwd拒绝不能启动PTY"));
  try {
    await assert.rejects(session.start(), (error) => {
      assert.equal(error.message, `终端 PTY 启动失败: terminal_id=${session.id}`);
      const [cwdError, ptyDiagnostic] = error.cause.errors;
      assert.equal(error.cause.cause, cwdError);
      assert.equal(ptyDiagnostic, diagnosticError);
      assert.equal(cwdError.message, "终端工作目录不存在或不是目录");
      const [original, cwdDiagnostic] = cwdError.cause.errors;
      assert.equal(cwdError.cause.cause, original);
      assert.equal(original.code, "ENOENT");
      assert.equal(cwdDiagnostic, diagnosticError);
      return true;
    });
    assert.equal(start.mock.callCount(), 0);
    assert.equal(diagnostic.mock.callCount(), 2);
    assert.equal(session.status, "created");
  } finally {
    await session.dispose();
  }
});

test("启动诊断次错保留启动清理三错、未知身份和显式重试", async (t) => {
  const session = completedSession(t, { status: "created" });
  const original = new Error("原启动失败");
  const cleanupError = new Error("原Worker未确认退出");
  const diagnosticError = new Error(`日志写入失败: ${session.cwd}/private-log`);
  const diagnostic = t.mock.method(process.stderr, "write", () => { throw diagnosticError; });
  const worker = { pid: 42002, startupCleanupResult: "terminated", async shutdown() { throw cleanupError; } };
  session.ptyProcess = worker;
  const states = [];
  session.manager.persist = async () => { states.push(session.toRecord()); };
  await assert.rejects(session.failStartup(original), (error) => {
    assert.equal(error.message, `终端启动失败且清理未完成: terminal_id=${session.id}`);
    assert(error instanceof AggregateError);
    const [failure, secondary] = error.errors;
    assert.equal(error.cause, failure);
    assert.equal(secondary, diagnosticError);
    assert.deepEqual(failure.errors, [original, cleanupError]);
    assert.equal(failure.cause, original);
    return true;
  });
  assert.equal(session.ptyProcess, worker);
  assert.equal(session.status, "created");
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(session.osPid, 42002);
  assert.equal(states.length, 1);
  assert.equal(original.creationReceipt, undefined);
  assert.equal(diagnostic.mock.callCount(), 1);
  assert(diagnostic.mock.calls[0].arguments[0].includes(inspect(cleanupError)));
  worker.shutdown = async () => {};
  await assert.rejects(session.failStartup(original), (error) => error === original);
  assert.equal(states.length, 3);
  assert.equal(session.ptyProcess, null);
  assert.equal(session.status, "terminated");
  assert.equal(session.reconciliationStatus, "verified");
  assert.deepEqual(original.creationReceipt, {
    resource_id: session.id, workspace_id: session.workspaceId, owner_revision: 1,
    outcome: "failed", resource: session.snapshot(),
  });
});

test("Worker 错误不进入持久状态，仍保留 unknown 与原始诊断", async (t) => {
  const session = completedSession(t);
  const original = new Error(`EIO: ${session.cwd}/private-worker`);
  const diagnostic = t.mock.method(process.stderr, "write", () => {});
  await session.handlePtyExit({ exitCode: -1, signal: 0, workerExit: true, error: original });
  assert.equal(session.snapshot().release_reason, "pty_worker_error");
  assert.equal(session.snapshot().status, "exited");
  assert.equal(session.snapshot().reconciliation_status, "unknown");
  assert.equal(JSON.stringify(session.toRecord()).includes(session.cwd), false);
  assert(diagnostic.mock.calls[0].arguments[0].includes(inspect(original)));
});

for (const cleanupResult of ["missing", "terminated", "force_killed", "still_running", "cleanup_failed", null]) {
  test(`启动失败仅凭正式进程树结果签发回执：${cleanupResult}`, async (t) => {
    const session = completedSession(t, { status: "created" });
    const original = new Error("受控启动失败");
    const shutdown = Promise.withResolvers();
    const stopped = ["missing", "terminated", "force_killed"].includes(cleanupResult);
    // 无原生身份的负例不能把替身PID送进真实OS；已交付PID的停止由真实PTY集成覆盖。
    const pid = stopped ? 42002 : null;
    session.ptyProcess = { pid, startupCleanupResult: cleanupResult, shutdown: () => shutdown.promise };
    const states = [];
    session.manager.persist = async () => { states.push(session.toRecord()); };
    const rejected = assert.rejects(session.failStartup(original), error => error === original);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(original.creationReceipt, undefined, "Worker退出确认前不能签回执");
    assert.equal(states.length, 1);
    assert.equal(states[0].reconciliation_status, "unknown");
    assert.equal(states[0].os_pid, pid);
    shutdown.resolve();
    await rejected;
    assert.equal(states.length, 2);
    assert.equal(session.ptyProcess, null);
    assert.equal(session.reconciliationStatus, stopped ? "verified" : "unknown");
    assert.equal(session.status, stopped ? "terminated" : "created");
    if (stopped) {
      assert.deepEqual(original.creationReceipt, {
        resource_id: session.id, workspace_id: session.workspaceId, owner_revision: 1,
        outcome: "failed", resource: session.snapshot(),
      });
      assert(Number.isFinite(Date.parse(states[1].ended_at)));
    } else assert.equal(original.creationReceipt, undefined);
  });
}

test("未取得原生PID的创建不能经管理停止伪造已终止", async t => {
  const session = completedSession(t, { status: "created", reconciliation_status: "unknown" });
  let shutdowns = 0;
  const worker = { pid: null, async shutdown() { shutdowns += 1; } };
  session.ptyProcess = worker;
  await assert.rejects(session.kill(), /原生创建身份尚未核实/);
  assert.equal(session.ptyProcess, worker);
  assert.equal(session.status, "created");
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(shutdowns, 0);
});

for (const cleanupResult of ["force_killed", "still_running", null]) {
  test(`原生身份在启动收尾期间到达仍按真实停止回执结算：${cleanupResult}`, async t => {
    const session = completedSession(t, { status: "created" });
    const original = new Error("原启动在身份交付前失败");
    const shutdown = Promise.withResolvers();
    const worker = { pid: null, startupCleanupResult: null, shutdown: () => shutdown.promise };
    session.ptyProcess = worker;
    const states = [];
    session.manager.persist = async () => { states.push(session.toRecord()); };
    const rejected = assert.rejects(session.failStartup(original), error => error === original);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(states[0].os_pid, null);
    assert.equal(states[0].reconciliation_status, "unknown");
    assert.equal(original.creationReceipt, undefined);
    worker.pid = 42002;
    worker.processIdentity = { pid: 42002, processGroupId: 42002, processSessionId: 42002, processStartTime: 9123 };
    worker.startupCleanupResult = cleanupResult;
    shutdown.resolve();
    await rejected;
    assert.equal(session.osPid, 42002);
    assert.equal(states.at(-1).os_pid, 42002);
    assert.equal(states.at(-1).process_group_id, 42002);
    assert.equal(states.at(-1).process_session_id, 42002);
    assert.equal(states.at(-1).process_start_time, 9123);
    assert.equal(session.ptyProcess, null);
    const stopped = cleanupResult === "force_killed";
    assert.equal(session.status, stopped ? "terminated" : "created");
    assert.equal(session.reconciliationStatus, stopped ? "verified" : "unknown");
    if (stopped) assert.deepEqual(original.creationReceipt, {
      resource_id: session.id, workspace_id: session.workspaceId, owner_revision: 1,
      outcome: "failed", resource: session.snapshot(),
    });
    else assert.equal(original.creationReceipt, undefined);
  });
}

test("正常启动持久化原Worker身份，不重新采样进程启动标记", async t => {
  const session = completedSession(t, { status: "created" });
  const identity = { pid: 42002, processGroupId: 42002, processSessionId: 42002, processStartTime: 9123 };
  t.mock.method(IsolatedPtyProcess.prototype, "start", async function () {
    this.pid = identity.pid;
    this.processIdentity = identity;
    return { pid: this.pid };
  });
  t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  await session.start();
  assert.equal(session.status, "running");
  assert.equal(session.osPid, identity.pid);
  assert.equal(session.processGroupId, identity.processGroupId);
  assert.equal(session.processSessionId, identity.processSessionId);
  assert.equal(session.processStartTime, identity.processStartTime);
  await session.dispose();
});

test("停止失败落盘包含收尾期间到达的原生身份，保留原异常", async t => {
  const session = completedSession(t, { status: "created", reconciliation_status: "unknown" });
  const identity = { pid: 42002, processGroupId: 42002, processSessionId: 42002, processStartTime: 9123 };
  const worker = { pid: 42002, processIdentity: undefined };
  session.ptyProcess = worker;
  const original = new Error("原停止失败");
  t.mock.method(session, "_terminateForRelease", async () => {
    worker.processIdentity = identity;
    throw original;
  });
  const saved = [];
  session.manager.persist = async () => { saved.push(session.toRecord()); };
  await assert.rejects(session.kill(), error => error === original);
  assert.equal(saved.length, 1);
  assert.equal(saved[0].os_pid, identity.pid);
  assert.equal(saved[0].process_group_id, identity.processGroupId);
  assert.equal(saved[0].process_session_id, identity.processSessionId);
  assert.equal(saved[0].process_start_time, identity.processStartTime);
  assert.equal(saved[0].status, "created");
  assert.equal(saved[0].reconciliation_status, "unknown");
  assert.equal(session.ptyProcess, worker);
  assert.equal(session.releasePromise, null);
});

for (const failSave of [false, true]) {
  test(`迟到身份遇到Worker关闭失败仍保存原事实并保留错误：${failSave}`, async t => {
    const session = completedSession(t, { status: "created" });
    const identity = { pid: 42002, processGroupId: 42002, processSessionId: 42002, processStartTime: 9123 };
    const original = new Error("原创建失败"), cleanup = new Error("原Worker尚未确认退出"), disk = new Error("身份写盘失败");
    t.mock.method(process.stderr, "write", () => {});
    const worker = { pid: null, processIdentity: undefined, async shutdown() {
      this.pid = identity.pid;
      this.processIdentity = identity;
      throw cleanup;
    } };
    session.ptyProcess = worker;
    const attempts = [];
    session.manager.persist = async () => {
      attempts.push(session.toRecord());
      if (failSave && attempts.length === 2) throw disk;
    };
    await assert.rejects(session.failStartup(original), error => {
      assert(error instanceof AggregateError);
      assert.equal(error.cause, original);
      assert.equal(error.errors[0], original);
      if (failSave) {
        assert(error.errors[1] instanceof AggregateError);
        assert.deepEqual(error.errors[1].errors, [cleanup, disk]);
        assert.equal(error.errors[1].cause, cleanup);
      } else assert.equal(error.errors[1], cleanup);
      return true;
    });
    assert.equal(attempts.length, 2);
    assert.equal(attempts[0].os_pid, null);
    assert.equal(attempts[1].os_pid, identity.pid);
    assert.equal(attempts[1].process_group_id, identity.processGroupId);
    assert.equal(attempts[1].process_session_id, identity.processSessionId);
    assert.equal(attempts[1].process_start_time, identity.processStartTime);
    assert.equal(attempts[1].reconciliation_status, "unknown");
    assert.equal(session.ptyProcess, worker);
    assert.equal(session.status, "created");
    assert.equal(original.creationReceipt, undefined);
  });
}

test("未实际执行停止时不能把原未知记录签为创建失败回执", async t => {
  const session = completedSession(t, { status: "exited", reconciliation_status: "unknown", os_pid: 42002 });
  const original = new Error("原Worker已丢失");
  await assert.rejects(session.failStartup(original), error => error === original);
  assert.equal(session.ptyProcess, null);
  assert.equal(session.status, "exited");
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(original.creationReceipt, undefined);
});

for (const status of ["created", "completed", "exited", "terminated", "deleted"]) {
  test(`无Worker的${status}未知记录仍核验持久身份后尝试管理停止`, async (t) => {
    const session = completedSession(t, {
      status, reconciliation_status: "unknown", os_pid: 42002,
      process_session_id: 42002, process_start_time: 123,
    });
    const original = new Error("原持久身份拒绝，禁止发送任何信号");
    const verify = t.mock.method(session, "verifyPersistedProcessIdentity", async () => { throw original; });
    const signals = t.mock.method(process, "kill", () => assert.fail("核验失败不得发送信号"));
    const saved = [];
    session.manager.persist = async () => { saved.push(session.toRecord()); };
    await assert.rejects(session.kill(), (error) => error === original);
    assert.equal(verify.mock.callCount(), 1);
    assert.deepEqual(verify.mock.calls[0].arguments, [42002]);
    assert.equal(signals.mock.callCount(), 0);
    assert.equal(session.ptyProcess, null);
    assert.equal(session.status, status);
    assert.equal(session.reconciliationStatus, "unknown");
    assert.equal(session.osPid, 42002);
    assert.equal(session.processSessionId, 42002);
    assert.equal(session.processStartTime, 123);
    assert.equal(session.releasePromise, null);
    assert.equal(saved.length, 1);
    assert.equal(saved[0].reconciliation_status, "unknown");
  });
}

test("无Worker的未知记录拒绝真实PID的过期启动身份，不发送信号", { skip: process.platform !== "linux" }, async (t) => {
  const identity = await readProcessStat(process.pid);
  assert(identity);
  const session = completedSession(t, {
    status: "exited", reconciliation_status: "unknown", os_pid: process.pid,
    process_group_id: identity.processGroupId,
    process_session_id: identity.processSessionId, process_start_time: identity.processStartTime + 1,
  });
  const signals = t.mock.method(process, "kill", () => assert.fail("过期身份不得发送信号"));
  await assert.rejects(session.delete(), /终端持久进程身份无法核实/);
  assert.equal(signals.mock.callCount(), 0);
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(session.status, "exited");
});

for (const field of ["os_pid", "process_session_id", "process_start_time"]) {
  test(`无Worker的未知记录缺少${field}时不能伪报管理停止`, async (t) => {
    const session = completedSession(t, {
      status: "exited", reconciliation_status: "unknown", os_pid: 42002,
      process_session_id: 42002, process_start_time: 123, [field]: null,
    });
    const signals = t.mock.method(process, "kill", () => assert.fail("身份不完整不得发送信号"));
    assert.equal(await session.kill(), false);
    assert.equal(session.status, "exited");
    assert.equal(session.reconciliationStatus, "unknown");
    assert.equal(signals.mock.callCount(), 0);
  });
}

test("启动收尾失败保留原Worker句柄、未知记录及两份异常", async (t) => {
  const session = completedSession(t, { status: "created" });
  const original = new Error("原启动失败");
  const cleanup = new Error("Worker未确认退出");
  t.mock.method(process.stderr, "write", () => {});
  const worker = { pid: 42002, startupCleanupResult: "terminated", async shutdown() { throw cleanup; } };
  session.ptyProcess = worker;
  const states = [];
  session.manager.persist = async () => { states.push(session.toRecord()); };
  await assert.rejects(session.failStartup(original), error => {
    assert(error instanceof AggregateError);
    assert.deepEqual(error.errors, [original, cleanup]);
    assert.equal(error.cause, original);
    return true;
  });
  assert.equal(session.ptyProcess, worker);
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(states.length, 1);
  assert.equal(original.creationReceipt, undefined);
});

for (const failedSave of [1, 2]) {
  test(`启动收尾第${failedSave}次落盘失败不能签回执`, async (t) => {
    const session = completedSession(t, { status: "created" });
    const original = new Error("原启动失败");
    const disk = new Error("结果落盘失败");
    const worker = { pid: 42002, startupCleanupResult: "terminated", async shutdown() {} };
    session.ptyProcess = worker;
    let saves = 0;
    session.manager.persist = async () => { if (++saves === failedSave) throw disk; };
    await assert.rejects(session.failStartup(original), error => error === disk);
    assert.equal(saves, failedSave);
    assert.equal(original.creationReceipt, undefined);
    if (failedSave === 1) assert.equal(session.ptyProcess, worker);
  });
}

test("退出清理失败只广播阶段，原始异常只留在诊断", async (t) => {
  const session = completedSession(t);
  const original = new Error(`EIO: ${session.cwd}/private-cleanup`);
  const diagnostic = t.mock.method(process.stderr, "write", () => {});
  const events = [];
  t.mock.method(session, "broadcast", (event) => events.push(event));
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => ({ pid: null }));
  t.mock.method(session, "handlePtyExit", async () => { throw original; });
  try {
    await session.start();
    session.ptyProcess._emitExit({ exitCode: -1, signal: 0, workerExit: true });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(session.toRecord().release_reason, "pty_exit_cleanup_failed");
    assert.equal(session.snapshot().status, "exited");
    assert.equal(events.length, 1);
    assert.equal(events[0].error, "pty_exit_cleanup_failed");
    assert.equal(JSON.stringify([session.snapshot(), events]).includes(session.cwd), false);
    assert(diagnostic.mock.calls[0].arguments[0].includes(inspect(original)));
  } finally {
    await session.dispose();
  }
});

test("Worker诊断次错保留原原因与未知身份，交由原退出回调结算", async (t) => {
  const session = completedSession(t);
  const original = new Error(`EIO: ${session.cwd}/private-worker`);
  const diagnosticError = new Error(`日志失败: ${session.cwd}/private-log`);
  const worker = {};
  session.ptyProcess = worker;
  t.mock.method(process.stderr, "write", () => { throw diagnosticError; });
  await assert.rejects(session.handlePtyExit({ exitCode: -1, signal: 0, workerExit: true, error: original }), (error) => {
    assert(error instanceof AggregateError);
    assert.deepEqual(error.errors, [original, diagnosticError]);
    assert.equal(error.cause, original);
    return true;
  });
  assert.equal(session.status, "exited");
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(session.releaseReason, "pty_worker_error");
  assert.equal(session.ptyProcess, worker);
});

test("Worker诊断和原停止信号同时失败时保留全部原因", { skip: process.platform !== "linux" }, async (t) => {
  const session = completedSession(t, { os_pid: process.pid });
  const original = new Error("原Worker失败");
  const diagnosticError = new Error("原Worker诊断失败");
  const cleanupError = Object.assign(new Error("原停止信号失败"), { code: "EPERM" });
  t.mock.method(process.stderr, "write", () => { throw diagnosticError; });
  // 当前进程只提供真实可读身份；所有信号均在系统调用前拒绝，不向OS发送任何信号。
  const signals = t.mock.method(process, "kill", () => { throw cleanupError; });
  await assert.rejects(session.handlePtyExit({ exitCode: -1, signal: 0, workerExit: true, error: original }), (error) => {
    assert.equal(error.message, `终端退出清理及诊断失败: terminal_id=${session.id}`);
    const [diagnosticFailure, secondary] = error.errors;
    assert.equal(error.cause, diagnosticFailure);
    assert.deepEqual(diagnosticFailure.errors, [original, diagnosticError]);
    assert.equal(diagnosticFailure.cause, original);
    assert(secondary.message.startsWith("终端进程操作失败:"));
    assert.equal(secondary.code, "EPERM");
    assert.deepEqual(secondary.cause.errors, [cleanupError, diagnosticError]);
    assert.equal(secondary.cause.cause, cleanupError);
    return true;
  });
  assert.equal(signals.mock.callCount(), 1);
  assert.deepEqual(signals.mock.calls[0].arguments, [process.pid, "SIGTERM"]);
  assert.equal(session.reconciliationStatus, "unknown");
});

async function startedExitCallback(t, session) {
  let onExit;
  t.mock.method(IsolatedPtyProcess.prototype, "onExit", (handler) => { onExit = handler; });
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => ({ pid: null }));
  t.after(() => session.dispose());
  await session.start();
  assert.equal(typeof onExit, "function");
  return onExit;
}

test("PTY自发退出等待原Worker确认后才释放句柄", async (t) => {
  const session = completedSession(t);
  const onExit = await startedExitCallback(t, session);
  const worker = session.ptyProcess;
  worker.workerState = "running";
  const entered = Promise.withResolvers();
  const exit = Promise.withResolvers();
  const shutdown = t.mock.method(worker, "shutdown", async (options) => {
    assert.deepEqual(options, { terminatePty: false });
    entered.resolve();
    await exit.promise;
    worker.workerState = "exited";
  });
  let finished = false;
  const pending = onExit({ exitCode: 0, signal: 0, workerExit: false })
    .then(() => { finished = true; });
  try {
    await entered.promise;
    assert.equal(finished, false);
    assert.equal(session.ptyProcess, worker);
  } finally {
    exit.resolve();
    await pending;
  }
  assert.equal(shutdown.mock.callCount(), 1);
  assert.equal(session.ptyProcess, null);
});

for (const action of ["kill", "delete"]) {
  test(`PTY退出等待Worker期间${action}不得提前成功或被迟到退出覆盖`, async (t) => {
    const session = completedSession(t);
    const onExit = await startedExitCallback(t, session);
    const worker = session.ptyProcess;
    worker.workerState = "running";
    const entered = Promise.withResolvers();
    const exit = Promise.withResolvers();
    t.mock.method(worker, "shutdown", async () => {
      entered.resolve();
      await exit.promise;
      worker.workerState = "exited";
    });
    const pendingExit = onExit({ exitCode: 0, signal: 0, workerExit: false });
    await entered.promise;
    let finished = false;
    const pendingManagement = session[action]().then(() => { finished = true; });
    try {
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(finished, false);
      assert.equal(session.reconciliationStatus, "unknown");
      assert.equal(session.ptyProcess, worker);
    } finally {
      exit.resolve();
      await Promise.all([pendingExit, pendingManagement]);
    }
    assert.equal(session.ptyProcess, null);
    assert.equal(session.reconciliationStatus, "verified");
    assert.equal(session.status, action === "kill" ? "terminated" : "deleted");
    assert.equal(session.releaseReason, action === "kill" ? "terminal_cancel" : "terminal_delete");
  });
}

test("PTY自发退出后Worker停止失败保留unknown及原句柄，同一管理链可重试", async (t) => {
  const session = completedSession(t);
  const onExit = await startedExitCallback(t, session);
  const worker = session.ptyProcess;
  worker.workerState = "running";
  const failure = new Error("原Worker退出未确认");
  const diagnostic = t.mock.method(process.stderr, "write", () => {});
  let attempts = 0;
  const shutdown = t.mock.method(worker, "shutdown", async (options) => {
    assert.deepEqual(options, { terminatePty: false });
    if (++attempts === 1) throw failure;
    worker.workerState = "exited";
  });
  await onExit({ exitCode: 0, signal: 0, workerExit: false });
  assert.equal(session.status, "exited");
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(session.ptyProcess, worker);
  assert.equal(session.releaseReason, "pty_exit_cleanup_failed");
  assert(diagnostic.mock.calls[0].arguments[0].includes(inspect(failure)));
  assert.equal(await session.kill(), true);
  assert.equal(shutdown.mock.callCount(), 2);
  assert.equal(session.ptyProcess, null);
  assert.equal(session.status, "terminated");
  assert.equal(session.reconciliationStatus, "verified");
});

for (const [failPrimaryLog, failPersist, failPersistLog] of [
  [false, false, false], [true, false, false], [false, true, false],
  [true, true, false], [false, true, true], [true, true, true],
]) {
  test(`退出失败结算等待发布：日志${failPrimaryLog}／存储${failPersist}／存储诊断${failPersistLog}`, async (t) => {
    const session = completedSession(t, { last_command_status: "running" });
    const original = new Error(`原退出失败: ${session.cwd}/private-worker`);
    const persistError = new Error(`原存储失败: ${session.cwd}/private-state`);
    const primaryLogError = new Error(`原退出诊断失败: ${session.cwd}/private-log`);
    const persistLogError = new Error(`原存储诊断失败: ${session.cwd}/private-log`);
    const onExit = await startedExitCallback(t, session);
    const worker = session.ptyProcess;
    t.mock.method(session, "handlePtyExit", async () => { throw original; });
    const diagnostics = t.mock.method(process.stderr, "write", (chunk) => {
      if (chunk.includes("退出清理失败") && failPrimaryLog) throw primaryLogError;
      if (chunk.includes("退出失败状态持久化失败") && failPersistLog) throw persistLogError;
    });
    const events = [];
    t.mock.method(session, "broadcast", (event) => events.push(event));
    const entered = Promise.withResolvers();
    const release = Promise.withResolvers();
    session.manager.persist = async () => {
      entered.resolve();
      await release.promise;
      if (failPersist) throw persistError;
    };
    let finished = false;
    const pending = Promise.allSettled([onExit({ exitCode: -1, signal: 0, workerExit: true })])
      .then((results) => { finished = true; return results[0]; });
    try {
      await entered.promise;
      assert.equal(finished, false, "状态发布完成前不能提前结算退出失败");
      assert.equal(session.status, "exited");
      assert.equal(session.lastCommandStatus, "exited");
      assert.equal(session.ptyProcess, worker, "未取得Worker退出确认不能因诊断失败丢弃句柄");
      assert.equal(session.reconciliationStatus, "unknown");
      assert.equal(events.length, 1);
      assert.equal(events[0].error, "pty_exit_cleanup_failed");
      assert.equal(events[0].exitCode, -1);
      assert.equal(JSON.stringify(events).includes(session.cwd), false);
    } finally {
      release.resolve();
    }
    const result = await pending;
    assert.equal(diagnostics.mock.callCount(), failPersist ? 2 : 1);
    assert(diagnostics.mock.calls[0].arguments[0].includes(inspect(original)));
    if (!failPrimaryLog && !failPersistLog) {
      assert.equal(result.status, "fulfilled", "原错误已有完整诊断，保持原后台处理合同");
    } else {
      assert.equal(result.status, "rejected");
      const failure = result.reason;
      assert(failure instanceof AggregateError);
      assert.equal(failure.cause, original);
      if (!failPersist) {
        assert.deepEqual(failure.errors, [original, primaryLogError]);
      } else {
        const [primary, secondary] = failure.errors;
        if (failPrimaryLog) {
          assert.deepEqual(primary.errors, [original, primaryLogError]);
          assert.equal(primary.cause, original);
        } else assert.equal(primary, original);
        if (failPersistLog) {
          assert.deepEqual(secondary.errors, [persistError, persistLogError]);
          assert.equal(secondary.cause, persistError);
        } else assert.equal(secondary, persistError);
      }
    }
  });
}

test("退出广播再次失败仍保留原退出及日志异常", async (t) => {
  const session = completedSession(t);
  const original = new Error("原退出失败");
  const diagnosticError = new Error("原退出诊断失败");
  const broadcastError = new Error("原退出广播失败");
  const onExit = await startedExitCallback(t, session);
  t.mock.method(session, "handlePtyExit", async () => { throw original; });
  t.mock.method(process.stderr, "write", () => { throw diagnosticError; });
  t.mock.method(session, "broadcast", () => { throw broadcastError; });
  await assert.rejects(onExit({ exitCode: -1, signal: 0, workerExit: true }), (error) => {
    assert(error instanceof AggregateError);
    assert.equal(error.cause, original);
    const [primary, secondary] = error.errors;
    assert.deepEqual(primary.errors, [original, diagnosticError]);
    assert.equal(primary.cause, original);
    assert.equal(secondary, broadcastError);
    return true;
  });
});

test("只有返回给模型的后台 execution 可以 claim steering", (t) => {
  const session = completedSession(t);
  const identity = { completionEventId: session.completionEventId, commandStartSequence: session.commandOutputBaselineSequence };

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
  const current = { completionEventId: session.completionEventId, commandStartSequence: session.commandOutputBaselineSequence };
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
  const identity = { completionEventId: session.completionEventId, commandStartSequence: session.commandOutputBaselineSequence };
  assert.equal(session.claimSteering(identity), true);
  session.finishSteering({ ...identity, dispatched: true });
  assert.equal(session.snapshot().steering_dispatched, true);
});

test("模型读取完成输出后抑制 terminal completion steering", async (t) => {
  const session = completedSession(t);
  session.markModelBackgrounded();

  await session.readModelOutput();

  assert.equal(session.snapshot().completion_observed_by_model, true);
  assert.equal(session.claimSteering({ completionEventId: session.completionEventId, commandStartSequence: session.commandOutputBaselineSequence }), false);
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
  mkdirSync(path.join(workspaceRoot, "src"));
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
      assert.equal(session.outputSequence, 1);
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
    const outputs = ["旧标记 __BOXTEAM_CMD_DONE_deadbeef__:9\n\x1b]0;旧标题\x1b\\", "\x1b[31m__BOXTEAM_CMD_DONE_ca", "fe__\x1b[0m:7\r\n\x1b]0;新标题\x1b\\", "尾部正文 __BOXTEAM_CMD_DONE_cafe__:8\n"];
    for (const [index, output] of outputs.entries()) {
      for (const receive of session.ptyProcess.ptyOutputHandlers) receive(output);
      if (index < 2) assert.equal(session.lastCommandStatus, "running");
    }
    assert.equal(scans, 3);
    assert.equal(session.lastCommandStatus, "completed");
    assert.equal(session.lastCommandExitCode, 7);
    assert.equal(session.outputSequence, 4);
    assert.equal(session.commandOutputBaselineSequence, 0);
    assert.equal(session.completionEventId, `terminal_completed:${session.id}:3`);
    assert.deepEqual(released, [{ status: "completed", commandStatus: "completed", reason: "command_completed" }]);
    assert.equal(session.outputMultiplexer.readAfter(0).output, outputs.join(""));
  } finally {
    await session.dispose();
  }
});

for (const action of ["kill", "delete"]) {
  test(`${action} 未确认 Worker 退出时保留原句柄并允许同一执行重试`, async (t) => {
    const session = completedSession(t);
    const failure = new Error("受控 Worker 退出未确认");
    let shutdowns = 0;
    // 这里只验证句柄和回执顺序；不向宿主进程发信号。
    const worker = { pid: null, async shutdown() { if (++shutdowns === 1) throw failure; } };
    session.ptyProcess = worker;
    const saved = [];
    session.manager.persist = async () => { saved.push(session.toRecord()); };
    const events = [];
    t.mock.method(session, "broadcast", (event) => events.push(event));
    await assert.rejects(session[action](), error => error === failure);
    assert.equal(session.ptyProcess, worker);
    assert.equal(session.reconciliationStatus, "unknown");
    assert.equal(saved.at(-1).reconciliation_status, "unknown");
    assert.equal(session.status, "running");
    assert.equal(session.releasePromise, null);
    assert.equal(events.length, 0);
    await session[action]();
    assert.equal(shutdowns, 2);
    assert.equal(session.ptyProcess, null);
    assert.equal(session.reconciliationStatus, "verified");
    assert.equal(session.status, action === "kill" ? "terminated" : "deleted");
  });
}

test("停止必须等待同一次 Worker 确认与最终写盘，不能提前发布完成事件", async (t) => {
  const session = completedSession(t);
  const workerExit = Promise.withResolvers();
  const saved = Promise.withResolvers();
  const saveStarted = Promise.withResolvers();
  let shutdowns = 0;
  session.ptyProcess = { pid: null, async shutdown() { shutdowns += 1; await workerExit.promise; } };
  session.manager.persist = async () => { saveStarted.resolve(); await saved.promise; };
  const events = [];
  t.mock.method(session, "broadcast", (event) => events.push(event));
  let finished = false;
  const stopping = session.kill().then(() => { finished = true; });
  const duplicate = session.kill();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(shutdowns, 1);
  assert.equal(finished, false);
  workerExit.resolve();
  await saveStarted.promise;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finished, false);
  assert.equal(events.length, 0);
  saved.resolve();
  await Promise.all([stopping, duplicate]);
  assert.equal(shutdowns, 1);
  assert.equal(events.length, 1);
  assert.equal(events[0].snapshot.reconciliation_status, "verified");
});

for (const action of ["kill", "delete"]) {
  test(`${action} 终态写盘失败保留原执行并可重试，不发布停止事件`, async (t) => {
    const session = completedSession(t);
    const disk = new Error("受控终态写盘失败");
    let shutdowns = 0;
    const worker = { pid: null, async shutdown() { shutdowns += 1; } };
    session.ptyProcess = worker;
    let writes = 0;
    session.manager.persist = async () => { if (++writes === 1) throw disk; };
    const events = [];
    t.mock.method(session, "broadcast", (event) => events.push(event));
    await assert.rejects(session[action](), error => error === disk);
    assert.equal(session.ptyProcess, worker);
    assert.equal(session.reconciliationStatus, "unknown");
    assert.equal(events.length, 0);
    assert.equal(writes, 2, "原失败后必须尝试持久化 unknown");
    await session[action]();
    assert.equal(shutdowns, 2);
    assert.equal(session.reconciliationStatus, "verified");
    assert.equal(session.ptyProcess, null);
    assert.equal(events.length, 1);
  });
}

test("停止与未知状态落盘都失败时保留原句柄与两份错误", async (t) => {
  const session = completedSession(t);
  const cleanup = new Error("原停止失败");
  const disk = new Error("未知状态落盘失败");
  const worker = { pid: null, async shutdown() { throw cleanup; } };
  session.ptyProcess = worker;
  session.manager.persist = async () => { throw disk; };
  await assert.rejects(session.kill(), error => {
    assert(error instanceof AggregateError);
    assert.deepEqual(error.errors, [cleanup, disk]);
    assert.equal(error.cause, cleanup);
    return true;
  });
  assert.equal(session.ptyProcess, worker);
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(session.status, "running");
});
