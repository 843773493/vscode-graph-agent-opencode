import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { EventEmitter } from "node:events";
import { syncBuiltinESMExports } from "node:module";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import { IsolatedPtyProcess } from "./isolatedPtyProcess.js";

async function createReadyWorker(t) {
  const worker = Object.assign(new EventEmitter(), { pid: 41001, connected: true });
  worker.send = t.mock.fn();
  worker.kill = t.mock.fn(() => true);
  t.mock.method(childProcess, "fork", () => worker);
  syncBuiltinESMExports();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.after(() => {
    worker.emit("exit", 0, null);
    t.mock.restoreAll();
    syncBuiltinESMExports();
    t.mock.timers.reset();
  });
  const isolated = new IsolatedPtyProcess();
  const startup = isolated.start({ command: "fixture-shell", args: [], options: {} });
  worker.emit("message", { type: "spawned", pid: 42001 });
  worker.emit("message", { type: "processIdentity", pid: 42001, identity: null });
  worker.emit("message", { type: "ready", pid: 42001 });
  assert.deepEqual(await startup, { pid: 42001 });
  return { isolated, worker };
}

test("未交付原生进程身份不能就绪", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const rejected = assert.rejects(start(), /尚未交付原生进程身份/);
  worker.emit("message", { type: "spawned", pid: 42002 });
  worker.emit("message", { type: "ready", pid: 42002 });
  await rejected;
  assert.equal(isolated.processIdentity, undefined);
  assert.equal(isolated.workerState, "stopping");
});

test("原Worker的进程身份在启动失败后仍保留且不能被后续消息替换", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const rejected = assert.rejects(start());
  worker.emit("error", new Error("身份交付前启动失败"));
  await rejected;
  const identity = { pid: 42002, processGroupId: 42002, processSessionId: 42002, processStartTime: 9123 };
  worker.emit("message", { type: "spawned", pid: 42002 });
  worker.emit("message", { type: "processIdentity", pid: 42002, identity });
  worker.emit("message", { type: "processIdentity", pid: 42002, identity: { ...identity, processStartTime: 9988 } });
  worker.emit("message", { type: "ready", pid: 42002 });
  worker.emit("exit", 0, null);
  worker.emit("message", { type: "processIdentity", pid: 42002, identity: null });
  assert.deepEqual(isolated.processIdentity, identity);
  assert.equal(isolated.workerState, "exited");
});

test("其它PID的进程身份拒绝启动且不能进入原记录", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const rejected = assert.rejects(start(), /原生进程身份与创建PID不一致/);
  worker.emit("message", { type: "spawned", pid: 42002 });
  worker.emit("message", { type: "processIdentity", pid: 42002, identity: { pid: 43003 } });
  await rejected;
  assert.equal(isolated.pid, 42002);
  assert.equal(isolated.processIdentity, undefined);
});

test("worker在宽限期内退出，关闭才完成且不发送强杀", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  const shutdown = isolated.shutdown();
  assert.deepEqual(worker.send.mock.calls.at(-1).arguments, [{ type: "shutdown", terminatePty: true }]);

  worker.emit("exit", 0, null);
  await shutdown;
  t.mock.timers.tick(6000);

  assert.equal(worker.kill.mock.callCount(), 0);
});

test("重叠关闭等待原Worker任务，失败结算后才能重试", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  const sendsBefore = worker.send.mock.callCount();
  const failures = Promise.allSettled([isolated.shutdown(), isolated.shutdown({ terminatePty: false })]);
  assert.equal(worker.send.mock.callCount(), sendsBefore + 1);
  t.mock.timers.tick(3000);
  t.mock.timers.tick(3000);
  const outcomes = await failures;
  assert.equal(outcomes[0].status, "rejected");
  assert.equal(outcomes[1].status, "rejected");
  assert.equal(outcomes[0].reason, outcomes[1].reason);
  assert.equal(worker.kill.mock.callCount(), 1);
  const retry = isolated.shutdown({ terminatePty: false });
  assert.equal(worker.send.mock.callCount(), sendsBefore + 2);
  worker.emit("exit", 0, null);
  await retry;
});

test("发送SIGKILL后仍等待worker退出事件，不能提前报告关闭成功", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  let resolved = false;
  const shutdown = isolated.shutdown().then(() => { resolved = true; });

  t.mock.timers.tick(3000);
  await setImmediate();

  assert.deepEqual(worker.kill.mock.calls.map(call => call.arguments), [["SIGKILL"]]);
  assert.equal(resolved, false, "发送信号不等于worker已经退出");
  worker.emit("exit", null, "SIGKILL");
  await shutdown;
  assert.equal(resolved, true);
});

test("强杀后仍无退出确认时限内明确失败，并释放本次退出监听", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  const listenersBefore = worker.listenerCount("exit");
  const shutdown = isolated.shutdown();
  const rejected = assert.rejects(shutdown, /隔离 PTY Worker 退出未确认.*worker_pid=41001/);

  t.mock.timers.tick(3000);
  t.mock.timers.tick(3000);
  await rejected;

  assert.equal(worker.listenerCount("exit"), listenersBefore);
  assert.equal(worker.kill.mock.callCount(), 1);
});

test("关闭IPC同步失败保留原异常，并清理退出监听与强杀定时器", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  const listenersBefore = worker.listenerCount("exit");
  const failure = new Error("fixture: IPC发送失败");
  worker.send.mock.mockImplementation(() => { throw failure; });

  await assert.rejects(isolated.shutdown(), error => error === failure);
  t.mock.timers.tick(6000);

  assert.equal(worker.listenerCount("exit"), listenersBefore);
  assert.equal(worker.kill.mock.callCount(), 0);
});

test("强杀同步失败保留原异常，并清理退出监听与确认定时器", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  const listenersBefore = worker.listenerCount("exit");
  const failure = new Error("fixture: 强杀失败");
  worker.kill.mock.mockImplementation(() => { throw failure; });
  const rejected = assert.rejects(isolated.shutdown(), error => error === failure);

  t.mock.timers.tick(3000);
  await rejected;
  t.mock.timers.tick(3000);

  assert.equal(worker.listenerCount("exit"), listenersBefore);
  assert.equal(worker.kill.mock.callCount(), 1);
});

function createStartingWorker(t) {
  const worker = Object.assign(new EventEmitter(), { pid: 41002, connected: true });
  worker.send = t.mock.fn();
  worker.kill = t.mock.fn(() => true);
  t.mock.method(childProcess, "fork", () => worker);
  syncBuiltinESMExports();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.after(() => {
    // 尚未就绪的替身没有真实进程；清理计时器，不补造 ready 或 exit 事件。
    t.mock.timers.reset();
    t.mock.restoreAll();
    syncBuiltinESMExports();
  });
  const isolated = new IsolatedPtyProcess();
  const start = () => isolated.start({ command: "fixture-shell", args: [], options: {} });
  return { isolated, worker, start };
}

test("启动IPC同步失败立即进入失败收尾，不遗留启动计时器", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const failure = new Error("fixture: 启动IPC发送失败");
  worker.send.mock.mockImplementation(() => { throw failure; });

  await assert.rejects(start(), error => error === failure);

  assert.deepEqual(worker.kill.mock.calls.map(call => call.arguments), [["SIGTERM"]]);
  assert.equal(isolated.workerState, "stopping");
  assert.equal(isolated.workerShutdownExpected, true);
  t.mock.timers.tick(20_000);
  assert.equal(worker.kill.mock.callCount(), 1);
  worker.emit("exit", 1, null);
  assert.equal(isolated.workerState, "exited");
});

test("启动IPC与停止请求都失败时保留两份原始异常", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const startupError = new Error("fixture: 启动IPC失败");
  const signalError = new Error("fixture: SIGTERM失败");
  worker.send.mock.mockImplementation(() => { throw startupError; });
  worker.kill.mock.mockImplementation(() => { throw signalError; });

  await assert.rejects(start(), error => {
    assert(error instanceof AggregateError);
    assert.deepEqual(error.errors, [startupError, signalError]);
    return true;
  });
  assert.equal(isolated.workerState, "stopping", "信号失败不能宣称worker已经退出");
  t.mock.timers.tick(20_000);
  assert.equal(worker.kill.mock.callCount(), 1);
});

for (const trigger of ["error", "startupError", "timeout"]) {
  test(`启动${trigger}后停止请求失败仍结束启动Promise`, async t => {
    const { isolated, worker, start } = createStartingWorker(t);
    const startupError = new Error("fixture: worker启动错误");
    const signalError = new Error("fixture: 启动收尾信号失败");
    const startup = start();
    const rejection = assert.rejects(startup, error => {
      assert(error instanceof AggregateError);
      assert.equal(error.errors[1], signalError);
      if (trigger === "error") assert.equal(error.errors[0], startupError);
      else assert.match(error.errors[0].message, trigger === "timeout" ? /启动超时/ : /worker启动错误/);
      return true;
    });
    worker.kill.mock.mockImplementation(() => { throw signalError; });

    assert.doesNotThrow(() => {
      if (trigger === "error") worker.emit("error", startupError);
      else if (trigger === "startupError") worker.emit("message", { type: "startupError", message: startupError.message });
      else t.mock.timers.tick(10_000);
    });
    await rejection;
    assert.equal(isolated.workerState, "stopping");
    t.mock.timers.tick(20_000);
    assert.equal(worker.kill.mock.callCount(), 1);
  });
}

test("就绪前退出直接报告退出事实，不向已退出worker再发信号", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const startup = start();
  const rejection = assert.rejects(startup, /就绪前退出: code=2, signal=null/);

  worker.emit("exit", 2, null);
  await rejection;
  t.mock.timers.tick(20_000);

  assert.equal(isolated.workerState, "exited");
  assert.equal(worker.kill.mock.callCount(), 0);
});

for (const cleanupResult of ["missing", "terminated", "force_killed", "still_running", "cleanup_failed", undefined]) {
  test(`就绪前PTY失败保留原PID及清理结果：${cleanupResult}`, async t => {
    const { isolated, worker, start } = createStartingWorker(t);
    const exits = [];
    isolated.onExit(event => exits.push(event));
    const startup = start();
    const rejected = assert.rejects(startup, /PTY启动即退出/);
    worker.emit("message", { type: "spawned", pid: 42002 });
    worker.emit("message", { type: "startupError", pid: 42002, cleanupResult, message: "PTY启动即退出" });
    await rejected;
    worker.emit("message", { type: "ready", pid: 43003 });
    worker.emit("message", { type: "startupError", pid: 43003, cleanupResult: "terminated" });
    worker.emit("exit", 1, null);
    t.mock.timers.tick(20_000);
    assert.equal(isolated.pid, 42002, "迟到消息不能替换失败创建的原身份");
    assert.equal(isolated.startupCleanupResult, cleanupResult ?? null);
    assert.equal(isolated.workerState, "exited");
    assert.deepEqual(exits, [], "就绪前的失败不能冒充运行中PTY退出");
    assert.equal(worker.kill.mock.callCount(), 1);
  });
}

test("普通Worker退出与无效PID不能提供PTY停止证明", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const rejected = assert.rejects(start(), /身份不明/);
  worker.emit("message", { type: "startupError", pid: true, cleanupResult: "missing", message: "身份不明" });
  await rejected;
  worker.emit("exit", 0, null);
  assert.equal(isolated.pid, null);
  assert.equal(isolated.startupCleanupResult, null);
});

for (const trigger of ["error", "timeout"]) {
  test(`启动${trigger}后仍接收同一Worker迟到的身份与停止回执`, async t => {
    const { isolated, worker, start } = createStartingWorker(t);
    const rejected = assert.rejects(start());
    if (trigger === "error") worker.emit("error", new Error("原生身份尚未交付"));
    else t.mock.timers.tick(10_000);
    await rejected;
    worker.emit("message", { type: "stopped", pid: 43003, cleanupResult: "terminated" });
    assert.equal(isolated.startupCleanupResult, null, "身份到达前不能签停止证明");
    worker.emit("message", { type: "spawned", pid: 42002 });
    worker.emit("message", { type: "spawned", pid: 43003 });
    worker.emit("message", { type: "ready", pid: 42002 });
    worker.emit("message", { type: "startupError", pid: 43003, cleanupResult: "terminated" });
    assert.equal(isolated.pid, 42002);
    assert.equal(isolated.workerState, "stopping");
    assert.equal(isolated.startupCleanupResult, null);
    worker.emit("message", { type: "stopped", pid: 42002, cleanupResult: "force_killed" });
    assert.equal(isolated.startupCleanupResult, "force_killed");
    assert.equal(worker.kill.mock.callCount(), 1, "迟到回执不能重放启动或停止信号");
  });
}

test("成功就绪后迟到的启动消息不能污染运行身份", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  worker.emit("message", { type: "spawned", pid: 43003 });
  worker.emit("message", { type: "startupError", pid: 44004, cleanupResult: "missing" });
  worker.emit("message", { type: "stopped", pid: 42001, cleanupResult: "force_killed" });
  worker.emit("message", { type: "ready", pid: 45005 });
  assert.equal(isolated.pid, 42001);
  assert.equal(isolated.startupCleanupResult, null);
  assert.equal(isolated.workerState, "running");
});

test("失败Worker已退出时不能再吸收迟到的原生身份", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const rejected = assert.rejects(start());
  worker.emit("exit", 1, null);
  await rejected;
  worker.emit("message", { type: "spawned", pid: 42002 });
  worker.emit("message", { type: "stopped", pid: 42002, cleanupResult: "terminated" });
  assert.equal(isolated.workerState, "exited");
  assert.equal(isolated.pid, null);
  assert.equal(isolated.startupCleanupResult, null);
  assert.equal(worker.kill.mock.callCount(), 0);
});

for (const trigger of ["error", "timeout", "exit"]) {
  test(`原生创建后${trigger}仍保留原PID，不伪造停止证明`, async t => {
    const { isolated, worker, start } = createStartingWorker(t);
    const rejected = assert.rejects(start());
    worker.emit("message", { type: "spawned", pid: 42002 });
    assert.equal(isolated.pid, 42002);
    assert.equal(isolated.workerState, "starting");
    if (trigger === "error") worker.emit("error", new Error("启动回执失败"));
    else if (trigger === "timeout") t.mock.timers.tick(10_000);
    else worker.emit("exit", 1, null);
    await rejected;
    worker.emit("message", { type: "spawned", pid: 43003 });
    worker.emit("message", { type: "ready", pid: 43003 });
    assert.equal(isolated.pid, 42002);
    assert.equal(isolated.startupCleanupResult, null);
  });
}

for (const pid of [null, true, 0, -1, 4.5]) {
  test(`非法原生PID不能进入就绪或停止事实：${pid}`, async t => {
    const { isolated, worker, start } = createStartingWorker(t);
    const rejected = assert.rejects(start(), /原生创建身份非法或重复/);
    worker.emit("message", { type: "spawned", pid });
    await rejected;
    assert.equal(isolated.pid, null);
    assert.equal(isolated.startupCleanupResult, null);
  });
}

for (const firstPid of [null, 42002]) {
  test(`就绪必须匹配已交付的唯一原生身份：${firstPid}`, async t => {
    const { isolated, worker, start } = createStartingWorker(t);
    const rejected = assert.rejects(start(), /就绪身份与原生创建不一致/);
    if (firstPid !== null) worker.emit("message", { type: "spawned", pid: firstPid });
    worker.emit("message", { type: "ready", pid: 43003 });
    await rejected;
    assert.equal(isolated.pid, firstPid);
    assert.equal(isolated.startupCleanupResult, null);
  });
}

test("重复原生身份拒绝启动且不替换原PID", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const rejected = assert.rejects(start(), /原生创建身份非法或重复/);
  worker.emit("message", { type: "spawned", pid: 42002 });
  worker.emit("message", { type: "spawned", pid: 42002 });
  await rejected;
  assert.equal(isolated.pid, 42002);
  assert.equal(isolated.startupCleanupResult, null);
});

test("其它PID的启动失败回执不能提供原PTY停止证明", async t => {
  const { isolated, worker, start } = createStartingWorker(t);
  const rejected = assert.rejects(start(), /错误身份回执/);
  worker.emit("message", { type: "spawned", pid: 42002 });
  worker.emit("message", { type: "startupError", pid: 43003, cleanupResult: "terminated", message: "错误身份回执" });
  await rejected;
  assert.equal(isolated.pid, 42002);
  assert.equal(isolated.startupCleanupResult, null);
});
