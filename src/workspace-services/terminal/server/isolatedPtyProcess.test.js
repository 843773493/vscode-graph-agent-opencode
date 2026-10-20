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
