import assert from "node:assert/strict";
import { cpSync, rmSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import test from "node:test";

import { TerminalManager } from "./terminalManager.js";
import { TerminalSession } from "./terminalSession.js";
import { IsolatedPtyProcess } from "./processes/isolatedPtyProcess.js";

async function prepareManager(t) {
  const root = path.join(process.cwd(), "out/tests/src/workspace-services/terminal/server/terminalShutdown/workspace", randomUUID());
  t.after(() => rmSync(root, { recursive: true, force: true }));
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/terminal_cwd_workspace"), root, { recursive: true });
  const { workspace_id: workspaceId } = JSON.parse(await readFile(path.join(root, ".boxteam/workspace-identity.json"), "utf8"));
  const manager = new TerminalManager({ workspaceRoot: root, workspaceId });
  await manager.init();
  return manager;
}

const intent = (manager) => ({ workspaceId: manager.workspaceId, terminalId: `term_${randomUUID().replaceAll("-", "")}`, ownerRevision: 3 });
const isTimeout = (error) => error.code === "terminal_manager_shutdown_timeout";

function assertNotCreated(error, options) {
  assert.equal(error.code, "terminal_manager_closing");
  assert.equal(error.statusCode, 503);
  assert.deepEqual(error.creationReceipt, {
    resource_id: options.terminalId, workspace_id: options.workspaceId, owner_revision: options.ownerRevision,
    outcome: "not_created",
  });
  return true;
}

test("关闭持有准备及排队创建；关闭后新请求不延长队尾，拒绝回执只属于合法 intent", async (t) => {
  const manager = await prepareManager(t);
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  t.mock.method(manager, "ensureExecutionCapacity", async () => { entered.resolve(); await release.promise; });
  const starts = t.mock.method(IsolatedPtyProcess.prototype, "start", async () => { throw new Error("不应启动"); });
  const first = intent(manager), queued = intent(manager);
  const requests = [manager.create(first)];
  await entered.promise;
  requests.push(manager.create(queued));
  const outcomes = Promise.allSettled(requests);
  try {
    await assert.rejects(manager.shutdown("test_shutdown", { shutdownWaitTimeoutMs: 5 }), isTimeout);
    const original = manager.shutdownPromise, tail = manager.capacityLock;
    const late = intent(manager);
    await assert.rejects(manager.create(late), (error) => assertNotCreated(error, late));
    for (const invalid of [{ ...late, ownerRevision: true }, { ...late, workspaceId: "wrong" }, { ...late, terminalId: "invalid" }]) {
      await assert.rejects(manager.create(invalid), (error) => { assert.equal(error.creationReceipt, undefined); return true; });
    }
    assert.equal(manager.capacityLock, tail);
    await assert.rejects(manager.shutdown("retry", { shutdownWaitTimeoutMs: 5 }), isTimeout);
    assert.equal(manager.shutdownPromise, original);
    release.resolve();
    const results = await outcomes;
    results.forEach((result, index) => { assert.equal(result.status, "rejected"); assertNotCreated(result.reason, [first, queued][index]); });
    await manager.shutdown();
    assert.equal(manager.terminalsById.size, 0);
    assert.equal(starts.mock.callCount(), 0);
  } finally {
    release.resolve();
    await outcomes;
  }
});

test("迟到就绪仍进入同一关闭；重复信号观察不重复启动或关闭，冲突身份不签未创建", async (t) => {
  const manager = await prepareManager(t);
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const starts = t.mock.method(IsolatedPtyProcess.prototype, "start", async () => { entered.resolve(); await release.promise; return { pid: null }; });
  const stops = t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async (options) => { assert.deepEqual(options, { terminatePty: false }); });
  const options = intent(manager);
  const creation = manager.create(options);
  const queued = manager.create(options);
  const collision = assert.rejects(queued, (error) => { assert.equal(error.creationReceipt, undefined); return true; });
  try {
    await entered.promise;
    await assert.rejects(manager.shutdown("original", { shutdownWaitTimeoutMs: 5 }), isTimeout);
    const original = manager.shutdownPromise;
    assert.equal(stops.mock.callCount(), 0);
    await assert.rejects(manager.shutdown("retry", { shutdownWaitTimeoutMs: 5 }), isTimeout);
    assert.equal(manager.shutdownPromise, original);
    release.resolve();
    const created = await creation;
    await collision;
    await manager.shutdown();
    await manager.shutdown();
    assert.equal(starts.mock.callCount(), 1);
    assert.equal(stops.mock.callCount(), 1);
    assert.equal(created.terminal_id, options.terminalId);
    const session = manager.get(options.terminalId);
    assert.equal(session.status, "running");
    assert.equal(session.ptyProcess, null);
    const record = JSON.parse(await readFile(manager.stateFile, "utf8")).terminals[0];
    assert.equal(record.reconciliation_status, "reconcile_required");
    assert.equal(record.release_reason, "original");
  } finally {
    release.resolve();
    await Promise.allSettled([creation, queued]);
  }
});

test("启动未知即使 worker 已退出仍阻止关闭，不将退出误作未创建或停止证明", async (t) => {
  const manager = await prepareManager(t);
  t.mock.method(process.stderr, "write", () => {});
  t.mock.method(IsolatedPtyProcess.prototype, "start", async () => { throw new Error("native 无 PID"); });
  const stops = t.mock.method(IsolatedPtyProcess.prototype, "shutdown", async () => {});
  const options = intent(manager);
  await assert.rejects(manager.create(options), (error) => { assert.equal(error.creationReceipt, undefined); return true; });
  for (let index = 0; index < 2; index += 1) {
    await assert.rejects(manager.shutdown(), (error) => {
      assert.equal(error.code, "terminal_shutdown_unverified_creation");
      assert.ok(error.message.includes(options.terminalId));
      return true;
    });
  }
  assert.equal(stops.mock.callCount(), 1);
  const session = manager.get(options.terminalId);
  assert.equal(session.reconciliationStatus, "unknown");
  assert.equal(session.status, "created");
});

test("首错不释放仍在途的整批关闭；全部排空后才允许重试原失败且不重发健康 worker 关闭", async (t) => {
  const manager = await prepareManager(t);
  const release = Promise.withResolvers();
  const failure = new Error("首项退出未确认");
  const counts = [0, 0];
  const sessions = [0, 1].map((index) => {
    const session = new TerminalSession({ manager, record: {
      terminal_id: intent(manager).terminalId, workspace_id: manager.workspaceId,
      transport_owner_revision: 3, reconciliation_status: "verified", cwd_relative: ".", status: "running",
    } });
    session.ptyProcess = { async shutdown(options) {
      assert.deepEqual(options, { terminatePty: false });
      counts[index] += 1;
      if (index === 0 && counts[index] === 1) throw failure;
      if (index === 1) await release.promise;
    } };
    manager.terminalsById.set(session.id, session);
    return session;
  });
  try {
    await assert.rejects(manager.shutdown("batch", { shutdownWaitTimeoutMs: 5 }), isTimeout);
    const original = manager.shutdownPromise;
    await assert.rejects(manager.shutdown("overlap", { shutdownWaitTimeoutMs: 5 }), isTimeout);
    assert.equal(manager.shutdownPromise, original);
    assert.deepEqual(counts, [1, 1]);
    assert.equal(sessions[0].reconciliationStatus, "unknown");
    release.resolve();
    await assert.rejects(original, (error) => error === failure);
    await manager.shutdown("explicit_retry");
    assert.deepEqual(counts, [2, 1]);
    assert.equal(sessions.every((session) => session.ptyProcess === null), true);
  } finally {
    release.resolve();
    await manager.shutdownPromise?.catch(() => {});
  }
});

test("整批关闭与最终写盘均失败时保留原顺序及 cause，显式重试不重复已完成副作用", async (t) => {
  const manager = await prepareManager(t);
  const stopFailure = new Error("worker 未退出"), writeFailure = new Error("终态写盘失败");
  const session = new TerminalSession({ manager, record: {
    terminal_id: intent(manager).terminalId, workspace_id: manager.workspaceId,
    transport_owner_revision: 3, reconciliation_status: "verified", cwd_relative: ".", status: "running",
  } });
  let stops = 0, writes = 0;
  session.ptyProcess = { async shutdown() { if (++stops === 1) throw stopFailure; } };
  manager.terminalsById.set(session.id, session);
  const writer = manager.stateStore.write.bind(manager.stateStore);
  t.mock.method(manager.stateStore, "write", async (payload) => { if (++writes === 1) throw writeFailure; await writer(payload); });
  await assert.rejects(manager.shutdown(), (error) => {
    assert.deepEqual(error.errors, [stopFailure, writeFailure]);
    assert.equal(error.cause, stopFailure);
    return true;
  });
  await manager.shutdown();
  await manager.shutdown();
  assert.equal(stops, 2);
  assert.equal(writes, 2);
  assert.equal(session.ptyProcess, null);
});
