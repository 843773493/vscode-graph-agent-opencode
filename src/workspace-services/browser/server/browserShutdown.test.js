import { afterAll, expect, spyOn, test } from "bun:test";
import { cpSync, rmSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { BrowserManager } from "./browserManager.js";
import { BrowserSession } from "./browserSession.js";

const workspaceRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/browser/server/browserShutdown/workspace", randomUUID());
afterAll(() => rmSync(workspaceRunRoot, { recursive: true, force: true }));

function createManager(writer = async () => undefined) {
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/browser_state_store_workspace"), workspaceRoot, { recursive: true });
  const manager = new BrowserManager({ workspaceRoot, workspaceId: "00000000-0000-4000-8000-000000000001" });
  manager.stateStore.write = writer;
  return manager;
}

function addBrowser(manager) {
  const browser = new BrowserSession({ manager, record: {
    browser_id: `browser_${randomUUID().replaceAll("-", "")}`,
    workspace_id: manager.workspaceId, transport_owner_revision: 1,
    status: "running", resource_state: "discarded", reconciliation_status: "verified",
  } });
  manager.browsersById.set(browser.id, browser);
  return browser;
}

test.each(["resources", "cleanup", "runtime", "persist"])("%s挂起时观察到期保留原任务，重入不重复关闭", async (phase) => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const checkpointError = new Error("检查点采集原失败");
  let writes = 0;
  const manager = createManager(async () => {
    writes += 1;
    if (phase === "persist") { entered.resolve(); await release.promise; }
  });
  const browser = addBrowser(manager), peer = addBrowser(manager);
  const checkpoints = [], closes = [];
  const originalCheckpoint = browser.checkpointForManagerShutdown.bind(browser);
  browser.checkpointForManagerShutdown = async (...args) => {
    checkpoints.push(args[0]);
    if (phase === "resources") { entered.resolve(); await release.promise; }
    if (phase === "cleanup") throw checkpointError;
    return await originalCheckpoint(...args);
  };
  const peerCheckpoint = peer.checkpointForManagerShutdown.bind(peer);
  const peerDone = Promise.withResolvers();
  peer.checkpointForManagerShutdown = async (...args) => {
    const result = await peerCheckpoint(...args);
    peerDone.resolve();
    return result;
  };
  const context = {};
  if (phase === "cleanup") browser.context = context;
  let contextCloses = 0;
  manager.runtimePool.releaseContext = async (observed) => {
    expect(observed).toBe(context);
    contextCloses += 1;
    entered.resolve();
    await release.promise;
  };
  manager.stateStore.deleteCheckpoint = async () => undefined;
  manager.runtimePool.shutdown = async () => {
    closes.push("runtime");
    if (phase === "runtime") { entered.resolve(); await release.promise; }
  };
  const first = manager.shutdown("original_reason", { shutdownWaitTimeoutMs: 20 });
  first.catch(() => undefined);
  try {
    await entered.promise;
    await peerDone.promise;
    await expect(first).rejects.toMatchObject({
      code: "browser_manager_shutdown_timeout", stage: phase === "cleanup" ? "resources" : phase,
      pending_browser_ids: ["resources", "cleanup"].includes(phase) ? [browser.id] : [],
    });
    const owner = manager.shutdownPromise;
    expect(owner).not.toBeNull();
    expect(browser.closingRequested).toBe(true);
    expect(peer.closingRequested).toBe(true);
    if (phase === "cleanup") {
      expect(browser.context).toBe(context);
      expect(browser.record.status).toBe("running");
      expect(browser.record.reconciliation_status).toBe("unknown");
    }
    await expect(manager.shutdown("new_reason", { shutdownWaitTimeoutMs: 5 })).rejects.toMatchObject({ code: "browser_manager_shutdown_timeout" });
    expect(manager.shutdownPromise).toBe(owner);
    expect(checkpoints).toEqual(["original_reason"]);
    release.resolve();
    if (phase === "cleanup") {
      await expect(owner).rejects.toMatchObject({ errors: [checkpointError] });
      expect(browser.record.status).toBe("lost");
      expect(browser.context).toBeNull();
    }
    await manager.shutdown();
    const settledWrites = writes;
    await manager.shutdown();
    expect(writes).toBe(settledWrites);
    expect(closes).toEqual(["runtime"]);
    expect(contextCloses).toBe(phase === "cleanup" ? 1 : 0);
    expect(checkpoints).toEqual(["original_reason"]);
  } finally {
    release.resolve();
    await Promise.allSettled([first, manager.shutdownPromise].filter(Boolean));
  }
});

test("先失败的资源不放弃仍挂起的关闭，原错按资源顺序收齐后才允许重试", async () => {
  const manager = createManager();
  const browsers = [addBrowser(manager), addBrowser(manager)];
  const errors = [new Error("第一项检查点失败"), new Error("第二项检查点失败")];
  const release = Promise.withResolvers();
  const attempts = [0, 0];
  for (const [index, browser] of browsers.entries()) {
    browser.checkpointForManagerShutdown = async () => {
      attempts[index] += 1;
      if (index === 0) await release.promise;
      throw errors[index];
    };
    browser.markManagerShutdownCheckpointFailed = async () => { browser.record.status = "lost"; };
  }
  let closes = 0;
  manager.runtimePool.shutdown = async () => { closes += 1; };
  try {
    await expect(manager.shutdown("original", { shutdownWaitTimeoutMs: 5 })).rejects.toMatchObject({ code: "browser_manager_shutdown_timeout", pending_browser_ids: [browsers[0].id] });
    const owner = manager.shutdownPromise;
    await expect(manager.shutdown("overlap", { shutdownWaitTimeoutMs: 5 })).rejects.toMatchObject({ code: "browser_manager_shutdown_timeout" });
    expect(manager.shutdownPromise).toBe(owner);
    expect(attempts).toEqual([1, 1]);
    expect(closes).toBe(0);
    release.resolve();
    await expect(owner).rejects.toMatchObject({ errors });
    expect(manager.shutdownPromise).toBeNull();
    await manager.shutdown();
    expect(closes).toBe(1);
    expect(attempts).toEqual([1, 1]);
  } finally {
    release.resolve();
    await Promise.allSettled([manager.shutdownPromise].filter(Boolean));
  }
});

test("共享运行时关闭成功但最后写盘失败，重试只补写盘并保留原错误", async () => {
  const original = new Error("最后状态写盘失败");
  let writes = 0, closes = 0;
  const manager = createManager(async () => { if (++writes === 1) throw original; });
  manager.runtimePool.shutdown = async () => { closes += 1; };
  await expect(manager.shutdown()).rejects.toBe(original);
  await manager.shutdown();
  await manager.shutdown();
  expect(closes).toBe(1);
  expect(writes).toBe(2);
});

test("重叠调用不重复进入同一资源的检查点和共享运行时关闭", async () => {
  const manager = createManager();
  const browser = addBrowser(manager);
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const reasons = [];
  browser.checkpointForManagerShutdown = async (reason) => { reasons.push(reason); entered.resolve(); await release.promise; };
  let closes = 0;
  manager.runtimePool.shutdown = async () => { closes += 1; };
  const first = manager.shutdown("first");
  await entered.promise;
  const second = manager.shutdown("overlap");
  try {
    await new Promise((resolve) => setImmediate(resolve));
    expect(reasons).toEqual(["first"]);
  } finally {
    release.resolve();
    await Promise.all([first, second]);
  }
  expect(closes).toBe(1);
});

test("默认30秒观察在资源、运行时和写盘阶段之间不重置", async () => {
  const timers = [];
  const originalTimer = globalThis.setTimeout;
  const timerSpy = spyOn(globalThis, "setTimeout").mockImplementation((callback, delay, ...args) => {
    if (delay === 30_000) timers.push(() => callback(...args));
    return originalTimer(callback, delay, ...args);
  });
  const gates = Array.from({ length: 3 }, () => ({ entered: Promise.withResolvers(), release: Promise.withResolvers() }));
  const pause = async (index) => { gates[index].entered.resolve(); await gates[index].release.promise; };
  const manager = createManager(() => pause(2));
  const browser = addBrowser(manager);
  browser.checkpointForManagerShutdown = () => pause(0);
  manager.runtimePool.shutdown = () => pause(1);
  const closing = manager.shutdown();
  closing.catch(() => undefined);
  try {
    for (const [index, gate] of gates.entries()) {
      await gate.entered.promise;
      expect(timers).toHaveLength(1);
      if (index < gates.length - 1) gate.release.resolve();
    }
    // 直接推进入口原timer，避免在单测中真实等待30秒。
    timers[0]();
    await expect(closing).rejects.toMatchObject({ code: "browser_manager_shutdown_timeout", stage: "persist" });
  } finally {
    for (const gate of gates) gate.release.resolve();
    await manager.shutdownPromise;
    await closing.catch(() => undefined);
    timerSpy.mockRestore();
  }
});

test("关闭失败时日志writer再次失败，两份原异常保持因果关系", async () => {
  const original = new Error("原关闭写盘失败"), diagnostic = new Error("日志writer失败");
  const manager = createManager(async () => { throw original; });
  manager.runtimePool.shutdown = async () => undefined;
  let attempted;
  const sink = spyOn(process.stderr, "write").mockImplementation((text) => { attempted = text; throw diagnostic; });
  try {
    const failure = await manager.shutdown().catch((error) => error);
    expect(failure).toBeInstanceOf(AggregateError);
    expect(failure.errors).toEqual([original, diagnostic]);
    expect(failure.cause).toBe(original);
    expect(attempted).toContain(original.message);
  } finally {
    sink.mockRestore();
  }
});
