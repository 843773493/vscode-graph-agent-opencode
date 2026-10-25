import { afterAll, describe, expect, test } from "bun:test";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { BrowserManager, BrowserTransportGrantRejected } from "./browserManager.js";
import { BrowserSession } from "./browserSession.js";

const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";
const workspaceRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/browser/server/browserManager/workspace", randomUUID());
// 只回收本次运行拥有的工作区；各用例仍获得独立的完整fixture副本。
afterAll(() => rmSync(workspaceRunRoot, { recursive: true, force: true }));

function createBrowserManagerWithStateWriter(writer, options = {}) {
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/default_test_workspace"), workspaceRoot, { recursive: true });
  mkdirSync(path.join(workspaceRoot, ".boxteam"), { recursive: true });
  writeFileSync(path.join(workspaceRoot, ".boxteam/workspace-identity.json"), JSON.stringify({ workspace_id: WORKSPACE_ID }));
  const manager = new BrowserManager({ workspaceRoot, workspaceId: WORKSPACE_ID, ...options });
  if (writer !== null) manager.stateStore.write = writer;
  return manager;
}
function createOptions() {
  return { workspaceId: WORKSPACE_ID, browserId: `browser_${randomUUID().replaceAll("-", "")}`, ownerRevision: 1 };
}

describe("浏览器状态持久化", () => {
  test("保存恢复记录不调用展示快照，真实落盘后重建同一资源快照", async () => {
    const manager = createBrowserManagerWithStateWriter(null);
    const session = new BrowserSession({ manager, record: {
      browser_id: "browser_0123456789abcdef0123456789abcdef",
      workspace_id: WORKSPACE_ID,
      status: "closed",
      reconciliation_status: "verified",
      transport_owner_revision: 1,
      document_revision: 2,
      operation_revision: 3,
      runtime_generation: 4,
      title: "恢复事实",
      url: "https://example.test/preserved",
      created_at: "2026-10-20T00:00:00.000Z",
      updated_at: "2026-10-20T00:00:01.000Z",
      device_profile: "iphone-13",
      device_orientation: "landscape",
      viewport_override: { width: 800, height: 600 },
      device_scale_factor_override: 2,
      network_profile_id: "slow-3g",
      downloads: [{ download_id: "download_abc123", filename: "报告.txt", status: "completed" }],
      last_operation: { operation_id: "op_saved", operation_revision: 12, status: "completed" },
      active_operation: null,
      client_count: 99,
      device_profiles: [{ id: "不应回存的展示副本" }],
    } });
    session.transportOwnerRevision = 5;
    session.documentRevision = 17;
    session.operationQueue.operationRevision = 13;
    session.chromiumInstanceGeneration = 7;
    const expected = structuredClone(session.snapshot());
    manager.browsersById.set(session.id, session);
    session.snapshot = () => { throw new Error("持久化不能计算展示快照"); };

    await manager.persist();

    const stored = JSON.parse(readFileSync(manager.stateStore.stateFile, "utf8")).browsers[0];
    expect(stored).toMatchObject({
      browser_id: session.id,
      transport_owner_revision: 5,
      document_revision: 17,
      operation_revision: 13,
      runtime_generation: 7,
      downloads: expected.downloads,
      last_operation: expected.last_operation,
    });
    for (const derived of ["client_count", "device_emulation", "device_profiles", "network_profiles", "participants", "pages", "active_page_id", "stream_metrics", "resource_protections", "pending_dialog", "pending_file_chooser"]) {
      expect(Object.hasOwn(stored, derived)).toBe(false);
    }
    const restored = new BrowserManager({ workspaceRoot: manager.workspaceRoot, workspaceId: WORKSPACE_ID });
    await restored.init();

    expect(restored.get(session.id).snapshot()).toEqual(expected);
  });

  test("重启时将带检查点的冻结浏览器恢复为可懒加载资源", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    manager.stateStore.readRecords = async () => [{
      browser_id: "browser_recoverable",
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "lost",
      resource_state: "frozen",
      release_reason: "browser_manager_startup_cleanup",
      ended_at: "2026-07-27T00:00:00.000Z",
    }];
    manager.stateStore.readCheckpoint = async () => ({
      version: 1,
      browser_id: "browser_recoverable",
      active_page_id: "page_active",
      pages: [{
        page_id: "page_active",
        title: "可恢复页面",
        url: "https://example.test/actual",
        requested_url: "https://example.test/requested",
        navigation_error: null,
        created_at: "2026-07-27T00:00:00.000Z",
      }],
    });

    await manager.init();

    const recovered = manager.get("browser_recoverable").record;
    expect(recovered).toMatchObject({
      status: "running",
      resource_state: "discarded",
      client_count: 0,
      ended_at: null,
      release_reason: "browser_manager_startup_checkpoint_recovery",
      error_message: null,
      discarded_pages: [{
        page_id: "page_active",
        title: "可恢复页面",
        url: "https://example.test/requested",
        actual_url: "https://example.test/actual",
        active: true,
      }],
    });
  });

  test("重启时缺少检查点的冻结浏览器明确标记为丢失", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    manager.stateStore.readRecords = async () => [{
      browser_id: "browser_missing_checkpoint",
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running",
      resource_state: "frozen",
    }];
    manager.stateStore.readCheckpoint = async () => null;

    await manager.init();

    expect(manager.get("browser_missing_checkpoint").record).toMatchObject({
      status: "lost",
      resource_state: "lost",
      client_count: 0,
      release_reason: "browser_checkpoint_missing_on_startup",
    });
  });

  test("重启时同版本 lost 冷回收记录恢复为可懒加载资源", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    manager.stateStore.readRecords = async () => [{
      browser_id: "browser_discarded",
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "lost",
      resource_state: "discarded",
      release_reason: "browser_manager_startup_checkpoint_recovery",
      ended_at: "2026-07-27T00:00:00.000Z",
    }];
    manager.stateStore.readCheckpoint = async () => ({
      version: 1,
      browser_id: "browser_discarded",
      active_page_id: "page_active",
      pages: [{
        page_id: "page_active",
        title: "冷回收页面",
        url: "about:blank",
        requested_url: "about:blank",
        navigation_error: null,
        created_at: "2026-07-27T00:00:00.000Z",
      }],
    });

    await manager.init();

    expect(manager.get("browser_discarded").record).toMatchObject({
      status: "running",
      resource_state: "discarded",
      ended_at: null,
      release_reason: "browser_manager_startup_checkpoint_recovery",
      error_message: null,
    });
  });

  test("同轮并发持久化合并为一次写入，写入仍严格串行", async () => {
    let active = 0;
    let maximumActive = 0;
    let writes = 0;
    const manager = createBrowserManagerWithStateWriter(async () => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      writes += 1;
      active -= 1;
    });

    await Promise.all([manager.persist(), manager.persist(), manager.persist()]);

    expect(writes).toBe(1);
    expect(maximumActive).toBe(1);
  });

  test("写入期间的连续更新只追加一次保存，所有等待方等到最新状态落盘", async () => {
    const firstEntered = Promise.withResolvers();
    const secondEntered = Promise.withResolvers();
    const releaseFirst = Promise.withResolvers();
    const releaseSecond = Promise.withResolvers();
    const savedVersions = [];
    let version = 1;
    let active = 0;
    let maximumActive = 0;
    let settled = 0;
    const manager = createBrowserManagerWithStateWriter(async (payload) => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      savedVersions.push(payload.browsers[0].version);
      if (savedVersions.length === 1) {
        firstEntered.resolve();
        await releaseFirst.promise;
      } else {
        secondEntered.resolve();
        await releaseSecond.promise;
      }
      active -= 1;
    });
    manager.browsersById.set("browser_updates", { toRecord: () => ({ version }) });
    const requests = [manager.persist().then(() => { settled += 1; })];
    try {
      await firstEntered.promise;
      for (version = 2; version <= 101; version += 1) {
        requests.push(manager.persist().then(() => { settled += 1; }));
      }
      version = 101;
      releaseFirst.resolve();
      await secondEntered.promise;
      expect(savedVersions).toEqual([1, 101]);
      expect(settled).toBe(0);
      releaseSecond.resolve();
      await Promise.all(requests);
      expect(savedVersions).toEqual([1, 101]);
      expect(settled).toBe(101);
      expect(maximumActive).toBe(1);
    } finally {
      releaseFirst.resolve();
      releaseSecond.resolve();
      await Promise.all(requests);
    }
  });

  test("后续保存期间再次更新仍会落盘，不丢掉最后一轮请求", async () => {
    const entered = Array.from({ length: 3 }, () => Promise.withResolvers());
    const release = Array.from({ length: 3 }, () => Promise.withResolvers());
    const savedVersions = [];
    let version = 1;
    const manager = createBrowserManagerWithStateWriter(async (payload) => {
      const index = savedVersions.length;
      savedVersions.push(payload.browsers[0].version);
      entered[index].resolve();
      await release[index].promise;
    });
    manager.browsersById.set("browser_updates", { toRecord: () => ({ version }) });
    const requests = [manager.persist()];
    try {
      await entered[0].promise;
      version = 2;
      requests.push(manager.persist());
      release[0].resolve();
      await entered[1].promise;
      version = 3;
      requests.push(manager.persist());
      release[1].resolve();
      await entered[2].promise;
      release[2].resolve();
      await Promise.all(requests);
      expect(savedVersions).toEqual([1, 2, 3]);
    } finally {
      for (const gate of release) gate.resolve();
      await Promise.all(requests);
    }
  });

  test.each(["record", "sync_writer", "async_writer"])("%s失败传给全部等待方，修复后可再次保存", async (failureStage) => {
    const failure = new Error(`保存失败: ${failureStage}`);
    let shouldFail = true;
    let saved = null;
    const manager = createBrowserManagerWithStateWriter((payload) => {
      if (shouldFail && failureStage === "sync_writer") throw failure;
      if (shouldFail && failureStage === "async_writer") return Promise.reject(failure);
      saved = structuredClone(payload.browsers);
      return Promise.resolve();
    });
    manager.browsersById.set("browser_failure", { toRecord: () => {
      if (shouldFail && failureStage === "record") throw failure;
      return { version: 2 };
    } });

    const results = await Promise.allSettled([manager.persist(), manager.persist(), manager.persist()]);

    expect(results.every((result) => result.status === "rejected" && result.reason === failure)).toBe(true);
    shouldFail = false;
    await manager.persist();
    expect(saved).toEqual([{ version: 2 }]);
  });

  test("恢复后仅稳定资源 ID 可以定位浏览器，内部标签页 ID 明确拒绝", () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const session = {
      id: "browser_recovered",
      pageEntries: new Map([["page_recovered_internal", {}]]),
      record: { page_id: "page_recovered_internal" },
    };
    manager.browsersById.set(session.id, session);

    expect(manager.get("browser_recovered")).toBe(session);
    expect(() => manager.get("page_recovered_internal")).toThrow("浏览器页面不存在");
  });

  test("传输 grant 只能消费一次，过期和撤销的版本均拒绝", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const browser = new BrowserSession({ manager, record: {
      browser_id: "browser_grant", workspace_id: WORKSPACE_ID,
      status: "running", transport_owner_revision: 1, reconciliation_status: "verified",
    } });
    manager.browsersById.set(browser.id, browser);
    const arguments_ = { ownerRevision: 1, association: { session_id: "ses_owner", thread_id: "thr_owner" } };
    const single = manager.issueTransportGrant(browser.id, arguments_);
    expect(manager.consumeTransportGrant(browser.id, single.grant_token).association).toEqual(arguments_.association);
    expect(() => manager.consumeTransportGrant(browser.id, single.grant_token)).toThrow("已消费");
    const expired = manager.issueTransportGrant(browser.id, arguments_);
    manager.transportGrants.get(expired.grant_token).expiresAtUnixMs = Date.now() - 1;
    expect(() => manager.consumeTransportGrant(browser.id, expired.grant_token)).toThrow("过期");
    const revoked = manager.issueTransportGrant(browser.id, arguments_);
    await manager.syncOwnerRevision(browser.id, 2);
    expect(() => manager.consumeTransportGrant(browser.id, revoked.grant_token)).toThrow("无效");
    expect(() => manager.issueTransportGrant(browser.id, arguments_)).toThrow("冲突");
    browser.record.reconciliation_status = "unknown";
    expect(() => manager.issueTransportGrant(browser.id, { ...arguments_, ownerRevision: 2 })).toThrow("尚未核实");
  });

  test("状态和版本竞争明确拒绝且不生成或改写 grant", () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const browser = new BrowserSession({ manager, record: {
      browser_id: "browser_rejected_grant", workspace_id: WORKSPACE_ID,
      status: "running", transport_owner_revision: 2, reconciliation_status: "verified",
    } });
    manager.browsersById.set(browser.id, browser);
    const existing = { browserId: browser.id, transportOwnerRevision: 2, expiresAtUnixMs: 1, association: null };
    manager.transportGrants.set("existing", existing);
    for (const [status, transportOwnerRevision] of [["running", 1], ["closed", 2], ["deleted", 2]]) {
      browser.record.status = status;
      let rejection;
      try {
        manager.issueTransportGrant(browser.id, { ownerRevision: transportOwnerRevision, association: null });
      } catch (error) {
        rejection = error;
      }
      expect(rejection).toBeInstanceOf(BrowserTransportGrantRejected);
      expect(rejection.code).toBe("browser_transport_grant_rejected");
      expect(rejection.operationOutcome).toBe("rejected");
      expect(rejection.browser.browser_id).toBe(browser.id);
      expect(rejection.browser.status).toBe(status);
      expect(rejection.browser.transport_owner_revision).toBe(2);
      expect(manager.transportGrants.size).toBe(1);
      expect(manager.transportGrants.get("existing")).toBe(existing);
    }
  });

  test.each(["prepare", "executing"])("撤销版本等待%s超时仍持久化新版本并保留原操作，迟到结果拒绝", async (phase) => {
    const writes = [];
    const manager = createBrowserManagerWithStateWriter(async (state) => { writes.push(structuredClone(state)); });
    const browser = new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId, workspace_id: WORKSPACE_ID,
      status: "running", transport_owner_revision: 1, reconciliation_status: "verified",
    } });
    manager.browsersById.set(browser.id, browser);
    const grant = manager.issueTransportGrant(browser.id, { ownerRevision: 1, association: null });
    let release;
    let enter;
    const barrier = new Promise((resolve) => { release = resolve; });
    const entered = new Promise((resolve) => { enter = resolve; });
    const effects = [];
    browser.wake = async () => { if (phase === "prepare") { enter(); await barrier; } };
    const operation = browser.runInteractiveOperation({ actor: "user", action: "old", transportOwnerRevision: 1 }, async () => {
      effects.push("entered");
      if (phase === "executing") { enter(); await barrier; }
      effects.push("completed");
    }).catch((error) => error);
    await entered;
    const accepted = [...browser.operationQueue.acceptedOperationExecutions];
    let outcome;
    const syncing = manager.syncOwnerRevision(browser.id, 2, { operationDrainTimeoutMs: 5 })
      .then(() => { outcome = "success"; }, (error) => { outcome = error; });
    try {
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(outcome).toMatchObject({ code: "browser_owner_revision_drain_timeout", statusCode: 408 });
      expect(writes.at(-1).browsers[0].transport_owner_revision).toBe(2);
      expect(() => manager.consumeTransportGrant(browser.id, grant.grant_token)).toThrow("无效");
      expect([...browser.operationQueue.acceptedOperationExecutions]).toEqual(accepted);
      expect(browser.record.status).toBe("running");
      expect(browser.closingRequested).toBe(false);
      expect(effects).toEqual(phase === "prepare" ? [] : ["entered"]);
    } finally {
      release();
      await syncing;
      expect(await operation).toMatchObject({ code: "browser_transport_revoked" });
    }
    await manager.syncOwnerRevision(browser.id, 2);
    expect(browser.operationQueue.acceptedOperationExecutions.size).toBe(0);
    expect(browser.transportOwnerRevision).toBe(2);
    expect(effects).toEqual(phase === "prepare" ? [] : ["entered", "completed"]);
  });

  test("短时间内的后台写入请求合并为一次", async () => {
    let writes = 0;
    const manager = createBrowserManagerWithStateWriter(async () => {
      writes += 1;
    });

    manager.schedulePersist(5);
    manager.schedulePersist(5);
    manager.schedulePersist(5);
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(writes).toBe(1);
  });

  test("跨BrowserSession的检查点捕获严格串行", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    let active = 0;
    let maximumActive = 0;
    const capture = () => manager.runCheckpointCapture(async () => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
    });

    await Promise.all([capture(), capture(), capture()]);

    expect(maximumActive).toBe(1);
  });

  test("正常关闭管理器时保留已冷回收浏览器及其检查点", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const browser = new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId,
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "discarded", reconciliation_status: "verified",
      checkpoint: { captured_at: "2026-10-24T00:00:00.000Z", size_bytes: 123 },
    } });
    manager.browsersById.set(browser.id, browser);
    const before = browser.toRecord();

    await manager.shutdown();

    expect(browser.closingRequested).toBe(true);
    expect(browser.toRecord()).toEqual(before);
    expect(browser.context).toBeNull();
    expect(manager.runtimePool.chromiumInstanceGeneration).toBe(0);
    await expect(browser.runInteractiveOperation({ actor: "user", action: "late" }, async () => {
      throw new Error("关闭后不应执行新操作");
    })).rejects.toMatchObject({ code: "browser_closing" });
  });

  test("管理器关闭不能跳过冷浏览器正在准备的已接纳操作", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const browser = new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId,
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "discarded", reconciliation_status: "verified",
    } });
    manager.browsersById.set(browser.id, browser);
    let releaseWake;
    let enterWake;
    const wakeEntered = new Promise((resolve) => { enterWake = resolve; });
    const wakeBarrier = new Promise((resolve) => { releaseWake = resolve; });
    browser.wake = async () => { enterWake(); await wakeBarrier; };
    const order = [];
    manager.runtimePool.shutdown = async () => { order.push("pool_shutdown"); };
    const operation = browser.runInteractiveOperation({ actor: "user", action: "preparing" }, async () => {
      order.push("operation");
    });
    await wakeEntered;
    const closing = manager.shutdown().then(() => { order.push("shutdown_done"); });
    try {
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(browser.closingRequested).toBe(true);
      expect(browser.pendingOperations).toBe(1);
      expect(browser.inFlightOperations).toBe(0);
      expect(order).toEqual([]);
    } finally {
      releaseWake();
      await operation;
      await closing;
    }
    expect(order).toEqual(["operation", "pool_shutdown", "shutdown_done"]);
    expect(browser.operationQueue.acceptedOperationExecutions.size).toBe(0);
    expect(browser.pendingOperations).toBe(0);
  });

  test("正常关闭管理器时将热浏览器转为可恢复检查点", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const shutdownReasons = [];
    manager.browsersById.set("browser_running", {
      id: "browser_running",
      status: "running",
      record: { resource_state: "background" },
      checkpointForManagerShutdown: async (reason) => {
        shutdownReasons.push(reason);
      },
      markManagerShutdownCheckpointFailed: async () => {
        throw new Error("不应进入检查点失败分支");
      },
      toRecord: () => ({
        browser_id: "browser_running",
        status: "running",
        resource_state: "discarded",
      }),
    });
    manager.runtimePool.shutdown = async () => undefined;

    await manager.shutdown("browser_manager_sigterm");

    expect(shutdownReasons).toEqual(["browser_manager_sigterm"]);
  });

  test.each(["prepare", "executing"])("管理器排空%s超时保留原操作与检查点，迟到结束后可重试关闭", async (phase) => {
    const saved = [];
    const manager = createBrowserManagerWithStateWriter(async (state) => { saved.push(state); });
    const browser = new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId,
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "background", reconciliation_status: "verified",
      checkpoint: { captured_at: "2026-10-24T00:00:00.000Z", size_bytes: 123 },
      agent_access_locked: true,
    } });
    manager.browsersById.set(browser.id, browser);
    const idle = new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId,
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "discarded", reconciliation_status: "verified",
    } });
    manager.browsersById.set(idle.id, idle);
    const before = browser.toRecord();
    const client = {};
    browser.clients.add(client);
    let enter;
    let release;
    const entered = new Promise((resolve) => { enter = resolve; });
    const barrier = new Promise((resolve) => { release = resolve; });
    const effects = [];
    browser.wake = async () => { if (phase === "prepare") { enter(); await barrier; } };
    browser.freeze = async () => { effects.push("freeze"); browser.record.resource_state = "frozen"; };
    browser.discard = async () => { effects.push("discard"); browser.record.resource_state = "discarded"; };
    manager.runtimePool.shutdown = async () => { effects.push("pool_shutdown"); };
    manager.stateStore.deleteCheckpoint = async () => { effects.push("delete_checkpoint"); };
    const checkpoint = browser.checkpointForManagerShutdown.bind(browser);
    browser.checkpointForManagerShutdown = (reason) => checkpoint(reason, { operationDrainTimeoutMs: 5 });
    const operation = browser.runInteractiveOperation({ actor: "user", action: phase }, async () => {
      if (phase === "executing") { enter(); await barrier; }
      effects.push("operation_done");
    });
    await entered;
    const accepted = [...browser.operationQueue.acceptedOperationExecutions];
    try {
      const failure = await manager.shutdown().catch((error) => error);
      expect(failure).toBeInstanceOf(AggregateError);
      expect(failure.errors).toHaveLength(1);
      expect(failure.errors[0].code).toBe("browser_shutdown_operation_drain_timeout");
      expect(idle.closingRequested).toBe(true);
      expect(idle.record.resource_state).toBe("discarded");
      expect(effects).toEqual([]);
      expect(saved).toEqual([]);
      expect(browser.record.status).toBe("running");
      expect(browser.record.reconciliation_status).toBe("verified");
      expect(browser.record.checkpoint).toEqual(before.checkpoint);
      expect(browser.record.agent_access_locked).toBe(true);
      expect(browser.clients.has(client)).toBe(true);
      expect([...browser.operationQueue.acceptedOperationExecutions]).toEqual(accepted);
    } finally {
      release();
      await operation;
      await manager.shutdown();
    }
    expect(effects).toEqual(["operation_done", "freeze", "discard", "pool_shutdown"]);
    expect(browser.operationQueue.acceptedOperationExecutions.size).toBe(0);
  });

  test("管理器等待首个资源前关闭本批其它资源的操作准入", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const browsers = Array.from({ length: 2 }, () => new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId,
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "discarded", reconciliation_status: "verified",
    } }));
    for (const browser of browsers) manager.browsersById.set(browser.id, browser);
    let release;
    let enter;
    const entered = new Promise((resolve) => { enter = resolve; });
    const barrier = new Promise((resolve) => { release = resolve; });
    browsers[0].wake = async () => { enter(); await barrier; };
    const original = browsers[0].runInteractiveOperation({ actor: "user", action: "preparing" }, async () => undefined);
    await entered;
    const closing = manager.shutdown();
    let lateExecutions = 0;
    try {
      expect(browsers.map((browser) => browser.closingRequested)).toEqual([true, true]);
      await expect(browsers[1].runInteractiveOperation({ actor: "user", action: "late" }, async () => {
        lateExecutions += 1;
      })).rejects.toMatchObject({ code: "browser_closing" });
      expect(lateExecutions).toBe(0);
      expect(browsers[0].operationQueue.acceptedOperationExecutions.size).toBe(1);
    } finally {
      release();
      await original;
      await closing;
    }
  });

  test("管理器本批资源共用排空预算，超时保留全部原操作并允许结束后重试", async () => {
    const manager = createBrowserManagerWithStateWriter(async () => undefined);
    const browsers = Array.from({ length: 4 }, () => new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId,
      workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "discarded", reconciliation_status: "verified",
    } }));
    const releases = [];
    const entered = [];
    const executions = [];
    let completed = 0;
    let poolCloses = 0;
    manager.runtimePool.shutdown = async () => { poolCloses += 1; };
    for (const [index, browser] of browsers.entries()) {
      manager.browsersById.set(browser.id, browser);
      const checkpoint = browser.checkpointForManagerShutdown.bind(browser);
      browser.checkpointForManagerShutdown = (reason, options = { operationDrainTimeoutMs: 200 }) => checkpoint(reason, options);
      if (index === 3) continue;
      let enter;
      const barrier = new Promise((resolve) => { releases.push(resolve); });
      entered.push(new Promise((resolve) => { enter = resolve; }));
      browser.wake = async () => { enter(); await barrier; };
      executions.push(browser.runInteractiveOperation({ actor: "user", action: "blocked" }, async () => { completed += 1; }));
    }
    await Promise.all(entered);
    const accepted = browsers.map((browser) => [...browser.operationQueue.acceptedOperationExecutions]);
    const before = browsers.map((browser) => browser.toRecord());
    const started = performance.now();
    try {
      const failure = await manager.shutdown("batch_timeout", { operationDrainTimeoutMs: 200 }).catch((error) => error);
      const elapsed = performance.now() - started;
      expect(failure).toBeInstanceOf(AggregateError);
      expect(failure.errors.map((error) => error.code)).toEqual(Array(3).fill("browser_shutdown_operation_drain_timeout"));
      for (const browser of browsers.slice(0, 3)) expect(failure.message).toContain(browser.id);
      expect(elapsed).toBeGreaterThanOrEqual(180);
      expect(elapsed).toBeLessThan(450);
      expect(browsers.map((browser) => [...browser.operationQueue.acceptedOperationExecutions])).toEqual(accepted);
      expect(browsers.map((browser) => browser.toRecord())).toEqual(before);
      expect(browsers.map((browser) => browser.closingRequested)).toEqual([true, true, true, true]);
      expect(poolCloses).toBe(0);
      expect(completed).toBe(0);
    } finally {
      for (const release of releases) release();
      await Promise.all(executions);
      await manager.shutdown();
    }
    expect(completed).toBe(3);
    expect(poolCloses).toBe(1);
    expect(browsers.every((browser) => browser.operationQueue.acceptedOperationExecutions.size === 0)).toBe(true);
  });

  test.each(["release_context", "delete_checkpoint", "persist", "unknown_persist"])("检查点失败后的%s收尾错误保留首因并继续其它资源，原身份可重试", async (stage) => {
    const states = [];
    let failing = true;
    const checkpointError = new Error("原检查点采集失败");
    const cleanupError = new Error(`收尾失败: ${stage}`);
    const unknownSaveError = new Error("unknown状态保存失败");
    const manager = createBrowserManagerWithStateWriter(async (state) => {
      if (failing && stage === "persist" && state.browsers.some((record) => record.status === "lost")) throw cleanupError;
      if (failing && stage === "unknown_persist" && state.browsers.some((record) => record.reconciliation_status === "unknown")) throw unknownSaveError;
      states.push(structuredClone(state));
    });
    const browser = new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId, workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "background", reconciliation_status: "verified",
      checkpoint: { captured_at: "2026-10-24T00:00:00.000Z", size_bytes: 123 },
    } });
    const peer = new BrowserSession({ manager, record: {
      browser_id: createOptions().browserId, workspace_id: WORKSPACE_ID, transport_owner_revision: 1,
      status: "running", resource_state: "discarded", reconciliation_status: "verified",
    } });
    manager.browsersById.set(browser.id, browser);
    manager.browsersById.set(peer.id, peer);
    browser.checkpointForManagerShutdown = async () => { throw checkpointError; };
    const context = {};
    browser.context = context;
    const attempted = [];
    const peerCheckpoint = peer.checkpointForManagerShutdown.bind(peer);
    peer.checkpointForManagerShutdown = async (...args) => { attempted.push(peer.id); return await peerCheckpoint(...args); };
    let releases = 0;
    let poolCloses = 0;
    manager.runtimePool.releaseContext = async (released) => {
      expect(released).toBe(context);
      releases += 1;
      if (failing && stage === "release_context") throw cleanupError;
    };
    manager.runtimePool.shutdown = async () => { poolCloses += 1; };
    manager.stateStore.deleteCheckpoint = async () => { if (failing && ["delete_checkpoint", "unknown_persist"].includes(stage)) throw cleanupError; };

    const failure = await manager.shutdown().catch((error) => error);

    expect(failure).toBeInstanceOf(AggregateError);
    expect(failure.errors).toHaveLength(1);
    const combined = failure.errors[0];
    expect(combined).toBeInstanceOf(AggregateError);
    expect(combined.cause).toBe(checkpointError);
    expect(combined.errors[0]).toBe(checkpointError);
    const failedCleanup = combined.errors[1];
    if (stage === "unknown_persist") {
      expect(failedCleanup).toBeInstanceOf(AggregateError);
      expect(failedCleanup.cause).toBe(cleanupError);
      expect(failedCleanup.errors).toEqual([cleanupError, unknownSaveError]);
    } else {
      expect(stage === "release_context" ? failedCleanup.cause : failedCleanup).toBe(cleanupError);
    }
    expect(attempted).toEqual([peer.id]);
    expect(poolCloses).toBe(0);
    expect(browser.record.status).toBe("running");
    expect(browser.record.reconciliation_status).toBe("unknown");
    expect(browser.snapshot().reconciliation_status).toBe("unknown");
    expect(browser.context).toBe(stage === "release_context" ? context : null);
    if (stage === "unknown_persist") expect(states).toEqual([]);
    else expect(states.at(-1).browsers.find((record) => record.browser_id === browser.id)).toMatchObject({
      status: "running", reconciliation_status: "unknown",
    });
    failing = false;
    const retry = await manager.shutdown().catch((error) => error);
    expect(retry).toBeInstanceOf(AggregateError);
    expect(retry.errors).toEqual([checkpointError]);
    expect(browser.record.status).toBe("lost");
    expect(browser.context).toBeNull();
    expect(browser.record.checkpoint).toBeNull();
    expect(releases).toBe(stage === "release_context" ? 2 : 1);
    expect(poolCloses).toBe(1);
    expect(attempted).toEqual([peer.id, peer.id]);
  });

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
    else manager.persist = async () => { throw cleanupError; };
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
