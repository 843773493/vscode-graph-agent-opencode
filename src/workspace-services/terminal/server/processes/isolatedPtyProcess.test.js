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
  worker.emit("message", { type: "ready", pid: 42001 });
  assert.deepEqual(await startup, { pid: 42001 });
  return { isolated, worker };
}

test("worker在宽限期内退出，关闭才完成且不发送强杀", async t => {
  const { isolated, worker } = await createReadyWorker(t);
  const shutdown = isolated.shutdown();
  assert.deepEqual(worker.send.mock.calls.at(-1).arguments, [{ type: "shutdown", terminatePty: true }]);

  worker.emit("exit", 0, null);
  await shutdown;
  t.mock.timers.tick(6000);

  assert.equal(worker.kill.mock.callCount(), 0);
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
