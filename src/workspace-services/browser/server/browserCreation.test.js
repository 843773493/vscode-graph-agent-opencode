import { afterAll, describe, expect, test } from "bun:test";
import { cpSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { BrowserManager } from "./browserManager.js";
import { BrowserSession } from "./browserSession.js";

const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";
const workspaceRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/browser/server/browserCreation/workspace", randomUUID());
// 只回收本次运行拥有的工作区；各用例仍获得独立的完整fixture副本。
afterAll(() => rmSync(workspaceRunRoot, { recursive: true, force: true }));

function createBrowserManagerWithStateWriter(writer, options = {}) {
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  cpSync(
    path.join(process.cwd(), "tests/fixtures/workspaces/browser_state_store_workspace"),
    workspaceRoot,
    { recursive: true },
  );
  const manager = new BrowserManager({ workspaceRoot, workspaceId: WORKSPACE_ID, ...options });
  if (writer !== null) manager.stateStore.write = writer;
  return manager;
}
function createOptions() {
  return { workspaceId: WORKSPACE_ID, browserId: `browser_${randomUUID().replaceAll("-", "")}`, ownerRevision: 1 };
}

test("Browser原生创建必须等待原unknown意图发布", async () => {
  const manager = createBrowserManagerWithStateWriter(null);
  const options = createOptions();
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const write = manager.stateStore.write.bind(manager.stateStore);
  let starts = 0, writes = 0;
  manager.stateStore.write = async snapshot => {
    await write(snapshot);
    if (++writes === 1) { entered.resolve(); await release.promise; }
  };
  const start = BrowserSession.prototype.start;
  BrowserSession.prototype.start = async function () {
    starts += 1;
    this.record.status = "running";
    this.record.reconciliation_status = "verified";
  };
  let creating;
  try {
    creating = manager.create(options);
    await entered.promise;
    expect(starts).toBe(0);
    const [record] = JSON.parse(readFileSync(manager.stateStore.stateFile, "utf8")).browsers;
    expect(record).toMatchObject({ browser_id: options.browserId, transport_owner_revision: 1,
      status: "created", reconciliation_status: "unknown" });
    expect(record).not.toHaveProperty("started_at");
  } finally {
    release.resolve();
    try { if (creating) await creating; } finally { BrowserSession.prototype.start = start; }
  }
  expect(starts).toBe(1);
  expect(manager.get(options.browserId).record.reconciliation_status).toBe("verified");
});

for (const afterPublish of [false, true]) {
  test(`Browser未派发意图${afterPublish ? "发布后" : "发布前"}失败沿原关闭链签未创建回执`, async () => {
    const manager = createBrowserManagerWithStateWriter(null);
    const options = createOptions();
    const original = new Error("原Browser意图保存失败");
    const write = manager.stateStore.write.bind(manager.stateStore);
    let writes = 0, starts = 0;
    manager.stateStore.write = async snapshot => {
      if (++writes === 1) {
        if (afterPublish) await write(snapshot);
        throw original;
      }
      await write(snapshot);
    };
    const start = BrowserSession.prototype.start;
    BrowserSession.prototype.start = async () => { starts += 1; };
    try { await expect(manager.create(options)).rejects.toBe(original); }
    finally { BrowserSession.prototype.start = start; }
    expect(starts).toBe(0);
    expect(original.creationReceipt).toEqual({ resource_id: options.browserId, workspace_id: WORKSPACE_ID,
      owner_revision: 1, outcome: "not_created" });
    const cold = new BrowserManager({ workspaceRoot: manager.workspaceRoot, workspaceId: WORKSPACE_ID });
    await cold.init();
    expect(cold.get(options.browserId).toRecord()).toMatchObject({ status: "failed", reconciliation_status: "verified",
      release_reason: "browser_creation_not_dispatched", runtime_generation: null });
  });
}

test("Browser未派发收尾再次失败保留原错链和unknown且不签回执", async () => {
  const manager = createBrowserManagerWithStateWriter(null);
  const options = createOptions();
  const failures = [new Error("首条意图保存失败"), new Error("关闭保存失败"), new Error("unknown保存失败")];
  let writes = 0, starts = 0;
  manager.stateStore.write = async () => { throw failures[writes++]; };
  const start = BrowserSession.prototype.start;
  BrowserSession.prototype.start = async () => { starts += 1; };
  try {
    try { await manager.create(options); throw new Error("应保留全部写入错误"); }
    catch (error) {
      expect(error).toBeInstanceOf(AggregateError);
      expect(error.cause).toBe(failures[0]);
      expect(error.errors[0]).toBe(failures[0]);
      expect(error.errors[1].errors).toEqual(failures.slice(1));
      expect(error.errors[1].cause).toBe(failures[1]);
      expect(error.creationReceipt).toBeUndefined();
    }
  } finally { BrowserSession.prototype.start = start; }
  expect(writes).toBe(3);
  expect(starts).toBe(0);
  expect(failures[0].creationReceipt).toBeUndefined();
  expect(manager.get(options.browserId).record).toMatchObject({ status: "failed", reconciliation_status: "unknown" });
});

test("Browser冷创建意图不能用空context假报关闭或删除", async () => {
  const manager = createBrowserManagerWithStateWriter(null);
  const options = createOptions();
  const session = new BrowserSession({ manager, record: {
    browser_id: options.browserId, workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
    status: "created", reconciliation_status: "unknown", created_at: "2026-10-31T00:00:00.000Z",
  } });
  manager.browsersById.set(session.id, session);
  await manager.persist();
  const cold = new BrowserManager({ workspaceRoot: manager.workspaceRoot, workspaceId: WORKSPACE_ID });
  await cold.init();
  const before = readFileSync(cold.stateStore.stateFile);
  for (const action of ["close", "delete"]) {
    await expect(cold[action](session.id)).rejects.toMatchObject({ code: "browser_creation_unverified", statusCode: 409 });
    expect(readFileSync(cold.stateStore.stateFile)).toEqual(before);
    expect(cold.get(session.id).record).toMatchObject({ status: "created", reconciliation_status: "unknown" });
    expect(cold.get(session.id).closingRequested).toBe(false);
  }
  expect(cold.runtimePool.snapshot()).toMatchObject({ connected: false, context_count: 0 });
});

describe("浏览器创建准入", () => {
  test("工作区逻辑浏览器达到硬上限时明确拒绝新建", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined, {
      maxLogicalBrowsersPerWorkspace: 2,
    });
    manager.browsersById.set("browser_a", { status: "running" });
    manager.browsersById.set("browser_b", { status: "running" });

    await expect(manager.create(createOptions())).rejects.toMatchObject({
      code: "browser_workspace_logical_limit_exceeded",
    });
  });

  test("严重内存压力下暂停创建新浏览器", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    manager.resourceGovernor = {
      snapshot: () => ({ last_sample: { level: "critical" } }),
    };

    await expect(manager.create(createOptions())).rejects.toMatchObject({
      code: "browser_creation_paused_memory_pressure",
    });
  });

  test("创建前为未生成检查点的资源预留工作区预算", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined, {
      checkpointReservationBytes: 4 * 1024 * 1024,
    });
    manager.stateStore.checkpointBudgetSnapshot = async () => ({
      used_bytes: 0,
      max_bytes: 6 * 1024 * 1024,
      remaining_bytes: 6 * 1024 * 1024,
    });
    manager.browsersById.set("browser_uncheckpointed", {
            status: "running",
      record: { resource_state: "background", checkpoint: null },
    });

    await expect(manager.create(createOptions())).rejects.toMatchObject({
      code: "browser_checkpoint_workspace_quota_exceeded",
    });
  });

  test("并发创建不能绕过工作区逻辑资源硬上限", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined, {
      maxLogicalBrowsersPerWorkspace: 1,
    });
    manager.stateStore.checkpointBudgetSnapshot = async () => ({
      used_bytes: 0,
      max_bytes: 2 * 1024 * 1024 * 1024,
      remaining_bytes: 2 * 1024 * 1024 * 1024,
    });
    manager.stateStore.deleteCheckpoint = async () => undefined;
    manager.persist = async () => undefined;
    let rejectFirstStart;
    manager.runtimePool.acquireContext = () => new Promise((resolve, reject) => {
      rejectFirstStart = reject;
    });

    const first = manager.create(createOptions());
    while (!rejectFirstStart) await Promise.resolve();
    const second = manager.create(createOptions());

    await expect(second).rejects.toMatchObject({ code: "browser_workspace_logical_limit_exceeded" });
    rejectFirstStart(new Error("结束并发创建测试"));
    await expect(first).rejects.toMatchObject({
      message: expect.stringContaining("stage=acquire_context"),
      cause: expect.objectContaining({ message: "结束并发创建测试" }),
    });
  });
});

test("创建拒绝回执绑定唯一 intent，重复身份不伪报未创建", async () => {
  const manager = createBrowserManagerWithStateWriter(async () => undefined, { maxLogicalBrowsersPerWorkspace: 1 });
  const options = createOptions();
  manager.browsersById.set("existing", { status: "running" });
  await expect(manager.create(options)).rejects.toMatchObject({ creationReceipt: {
    resource_id: options.browserId, workspace_id: WORKSPACE_ID, owner_revision: 1, outcome: "not_created",
  } });
  expect(manager.browsersById.has(options.browserId)).toBe(false);
  manager.browsersById.set(options.browserId, { status: "running" });
  try { await manager.create(options); throw new Error("应拒绝重复身份"); }
  catch (error) {
    expect(error.message).toContain("唯一身份");
    expect(error.creationReceipt).toBeUndefined();
  }
});


for (const stage of ["delete_checkpoint", "persist_failure"]) {
  test(`启动清理未完成时不签发创建失败回执: ${stage}`, async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const options = createOptions();
    const cleanupError = new Error(`injected ${stage}`);
    manager.runtimePool.acquireContext = async () => { throw new Error("injected startup failure"); };
    if (stage === "delete_checkpoint") manager.stateStore.deleteCheckpoint = async () => { throw cleanupError; };
    else {
      let saves = 0;
      manager.persist = async () => { if (++saves > 1) throw cleanupError; };
    }
    try { await manager.create(options); throw new Error("应报告启动与清理失败"); }
    catch (error) {
      expect(error).toBeInstanceOf(AggregateError);
      if (stage === "persist_failure") {
        expect(error.errors[1]).toBeInstanceOf(AggregateError);
        expect(error.errors[1].errors).toEqual([cleanupError, cleanupError]);
        expect(error.errors[1].cause).toBe(cleanupError);
      } else expect(error.errors[1]).toBe(cleanupError);
      expect(error.creationReceipt).toBeUndefined();
    }
    expect(manager.browsersById.has(options.browserId)).toBe(true);
  });
}


describe("关闭时的浏览器创建", () => {
  test("关闭后拒绝新创建，身份冲突仍不能签发未创建回执", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    let budgetReads = 0;
    manager.stateStore.checkpointBudgetSnapshot = async () => { budgetReads += 1; throw new Error("关闭后不应读取配额"); };
    await manager.shutdown();
    const options = createOptions();
    await expect(manager.create(options)).rejects.toMatchObject({
      code: "browser_manager_closing", statusCode: 503,
      creationReceipt: { resource_id: options.browserId, workspace_id: WORKSPACE_ID, owner_revision: 1, outcome: "not_created" },
    });
    expect(budgetReads).toBe(0);
    expect(manager.browsersById.size).toBe(0);
    manager.browsersById.set(options.browserId, { status: "closed" });
    try { await manager.create(options); throw new Error("应拒绝重复身份"); }
    catch (error) { expect(error.message).toContain("唯一身份"); expect(error.creationReceipt).toBeUndefined(); }
  });

  test("配额等待中的创建和排队创建不能越过关闭准入，超时不关闭共享池", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const entered = Promise.withResolvers();
    const budget = Promise.withResolvers();
    const originalBudget = manager.stateStore.checkpointBudgetSnapshot.bind(manager.stateStore);
    manager.stateStore.checkpointBudgetSnapshot = async () => { entered.resolve(); await budget.promise; return await originalBudget(); };
    let poolStops = 0;
    manager.runtimePool.shutdown = async () => { poolStops += 1; };
    const first = createOptions();
    const second = createOptions();
    const creating = manager.create(first);
    creating.catch(() => undefined);
    await entered.promise;
    const queued = manager.create(second);
    queued.catch(() => undefined);
    try {
      await expect(manager.shutdown("blocked_quota", { operationDrainTimeoutMs: 20 })).rejects.toMatchObject({ code: "browser_shutdown_creation_drain_timeout" });
      expect(poolStops).toBe(0);
      expect(manager.browsersById.size).toBe(0);
      const late = createOptions();
      await expect(manager.create(late)).rejects.toMatchObject({ code: "browser_manager_closing", creationReceipt: { resource_id: late.browserId, outcome: "not_created" } });
    } finally { budget.resolve(); }
    for (const [execution, options] of [[creating, first], [queued, second]]) {
      await expect(execution).rejects.toMatchObject({ code: "browser_manager_closing", creationReceipt: { resource_id: options.browserId, outcome: "not_created" } });
    }
    expect(manager.browsersById.size).toBe(0);
    await manager.shutdown();
    expect(poolStops).toBe(1);
  });

  for (const timeout of [false, true]) {
    test(`已接纳创建完成后才检查点并关闭，保留原创建身份: timeout=${timeout}`, async () => {
      const manager = createBrowserManagerWithStateWriter(async () => undefined);
      const entered = Promise.withResolvers();
      const startup = Promise.withResolvers();
      const order = [];
      const start = BrowserSession.prototype.start;
      BrowserSession.prototype.start = async function () {
        entered.resolve(this);
        await startup.promise;
        this.record.status = "running";
        order.push("created");
        return this.snapshot();
      };
      let closing;
      let creating;
      try {
        const options = createOptions();
        creating = manager.create(options);
        creating.catch(() => undefined);
        const browser = await entered.promise;
        browser.checkpointForManagerShutdown = async () => { expect(browser.closingRequested).toBe(true); order.push("checkpoint"); };
        manager.runtimePool.shutdown = async () => { order.push("pool_stop"); };
        closing = manager.shutdown("pending_start", { operationDrainTimeoutMs: timeout ? 20 : 1000 });
        closing.catch(() => undefined);
        expect(browser.closingRequested).toBe(true);
        await expect(browser.runInteractiveOperation({ actor: "user", action: "during_startup_shutdown" }, async () => {
          throw new Error("关闭开始后不能接纳创建中的Browser交互");
        })).rejects.toMatchObject({ code: "browser_closing" });
        if (timeout) {
          await expect(closing).rejects.toMatchObject({ code: "browser_shutdown_creation_drain_timeout" });
          expect(manager.get(browser.id)).toBe(browser);
          expect(browser.record.status).toBe("created");
        } else {
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        expect(order).toEqual([]);
        startup.resolve();
        const snapshot = await creating;
        expect(snapshot.browser_id).toBe(options.browserId);
        expect(browser.closingRequested).toBe(true);
        if (timeout) closing = manager.shutdown();
        await closing;
        expect(order).toEqual(["created", "checkpoint", "pool_stop"]);
      } finally {
        startup.resolve();
        await Promise.allSettled([creating, closing].filter(Boolean));
        BrowserSession.prototype.start = start;
      }
    });
  }

  test("关闭等到原启动失败收尾结束，未核实收尾不能关闭共享池", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const entered = Promise.withResolvers();
    const cleanup = Promise.withResolvers();
    const failure = new Error("startup refused");
    const cleanupFailure = new Error("checkpoint delete blocked");
    manager.runtimePool.acquireContext = async () => { throw failure; };
    manager.stateStore.deleteCheckpoint = async () => { entered.resolve(); await cleanup.promise; throw cleanupFailure; };
    let poolStops = 0;
    manager.runtimePool.shutdown = async () => { poolStops += 1; };
    const options = createOptions();
    const creating = manager.create(options);
    creating.catch(() => undefined);
    await entered.promise;
    const closing = manager.shutdown();
    closing.catch(() => undefined);
    try {
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(poolStops).toBe(0);
    } finally { cleanup.resolve(); }
    await expect(creating).rejects.toMatchObject({ errors: [expect.any(Error), cleanupFailure] });
    await expect(closing).rejects.toMatchObject({ code: "browser_shutdown_unverified_creation" });
    expect(poolStops).toBe(0);
    expect(manager.get(options.browserId).record.reconciliation_status).toBe("unknown");
    manager.stateStore.deleteCheckpoint = async () => undefined;
    await manager.close(options.browserId);
    await manager.shutdown();
    expect(poolStops).toBe(1);
  });
});
