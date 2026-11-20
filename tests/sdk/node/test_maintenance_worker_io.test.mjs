import { after, test } from "node:test";
import assert from "node:assert/strict";
import { Worker } from "node:worker_threads";
import { mkdir, readFile, rm } from "node:fs/promises";
import path from "node:path";
import { startMaintenance } from "@boxteam/plugin-sdk/maintenance";

const output = path.resolve("out/tests/sdk/node/test_maintenance_worker_io");
const workspace = path.join(output, "workspace");
const workers = new Set();

async function startFileWriter(target, failureText = null) {
  const source = `
    import { parentPort, workerData } from 'node:worker_threads';
    import { open } from 'node:fs/promises';
    const handle = await open(workerData.target, 'a');
    parentPort.postMessage({ type: 'ready' });
    parentPort.once('message', async (message) => {
      if (message !== 'release') throw new Error('invalid worker barrier message');
      await handle.writeFile(workerData.contents);
      await handle.sync();
      await handle.close();
      parentPort.postMessage({ type: 'written', failureText: workerData.failureText });
    });
  `;
  const worker = new Worker(new URL(`data:text/javascript,${encodeURIComponent(source)}`), {
    type: "module", workerData: { target, contents: "settled\n", failureText },
  });
  workers.add(worker);
  let readyResolve;
  let readyReject;
  let writtenResolve;
  let writtenReject;
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const written = new Promise((resolve, reject) => { writtenResolve = resolve; writtenReject = reject; });
  worker.on("message", message => {
    if (message.type === "ready") readyResolve();
    if (message.type === "written") writtenResolve(message.failureText);
  });
  worker.on("error", error => { readyReject(error); writtenReject(error); });
  worker.on("exit", code => {
    if (code !== 0) {
      const error = new Error(`worker exited ${code}`);
      readyReject(error);
      writtenReject(error);
    }
  });
  await ready;
  return { worker, written, release: () => worker.postMessage("release") };
}

async function until(predicate) {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("维护回调未及时启动");
    await new Promise(resolve => setTimeout(resolve, 5));
  }
}

async function resetWorkspace() {
  await rm(workspace, { recursive: true, force: true });
  await mkdir(workspace, { recursive: true });
}

after(async () => {
  await Promise.all([...workers].map(worker => worker.terminate()));
  await rm(workspace, { recursive: true, force: true });
});

test("真实 Worker 文件写入由并发 close 共用的结果排空，且错误仅首次交付", async () => {
  await resetWorkspace();
  const target = path.join(workspace, "state.log");
  const events = [];
  let writer;
  const maintenance = startMaintenance({
    identity: { instance_id: "worker-io" },
    reportError() {},
    tasks: [{
      id: "persist-state", interval_ms: 60_000, run_immediately: true,
      run: async () => {
        writer = await startFileWriter(target, "write completed with worker failure");
        events.push("worker-opened");
        const failureText = await writer.written;
        if (failureText) throw new Error(failureText);
      },
    }],
  });

  await until(() => writer !== undefined);
  const firstClose = maintenance.close();
  const concurrentClose = maintenance.close();
  assert.strictEqual(concurrentClose, firstClose);
  const shutdown = (async () => {
    try { await firstClose; }
    finally { events.push("dependency-released"); }
  })();
  let settled = false;
  void shutdown.finally(() => { settled = true; }).catch(() => {});
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(settled, false, "close 应等待 Worker 持有的在途文件操作");
  events.push("release-requested");
  writer.release();
  await assert.rejects(firstClose, /write completed with worker failure/);
  await assert.rejects(shutdown, /write completed with worker failure/);
  assert.deepEqual(events, ["worker-opened", "release-requested", "dependency-released"]);
  assert.equal(await readFile(target, "utf8"), "settled\n");
  await maintenance.close();
  assert.equal(await readFile(target, "utf8"), "settled\n", "再次 close 不重放已结算的回调");
});

test("启动后续步骤失败时，owner close 仍等待真实 Worker 写入后再释放依赖", async () => {
  await resetWorkspace();
  const target = path.join(workspace, "state.log");
  const events = [];
  let writer;
  const maintenance = startMaintenance({ identity: {}, reportError() {}, tasks: [{
    id: "persist-before-ready", interval_ms: 60_000, run_immediately: true,
    run: async () => {
      writer = await startFileWriter(target);
      events.push("worker-opened");
      await writer.written;
    },
  }] });
  await until(() => writer !== undefined);
  const startupFailure = new Error("later initialization failed");
  const startup = (async () => {
    try {
      throw startupFailure;
    } catch (error) {
      events.push("startup-failed");
      try {
        await maintenance.close();
      } finally {
        events.push("dependency-released");
      }
      throw error;
    }
  })();
  await until(() => events.includes("startup-failed"));
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(events, ["worker-opened", "startup-failed"]);
  writer.release();
  await assert.rejects(startup, error => error === startupFailure);
  await writer.written;
  assert.deepEqual(events, ["worker-opened", "startup-failed", "dependency-released"]);
  assert.equal(await readFile(target, "utf8"), "settled\n");
});
