import { describe, expect, test } from "bun:test";
import { cpSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { BrowserManager } from "./browserManager.js";
import { BrowserSession } from "./browserSession.js";

const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";
function managerWithWriter(writer, options = {}) {
  const workspaceRoot = path.join(process.cwd(), "out/tests/src/workspace-services/browser/server/browserManager/workspace", randomUUID());
  cpSync(path.join(process.cwd(), "tests/fixtures/workspaces/default_test_workspace"), workspaceRoot, { recursive: true });
  mkdirSync(path.join(workspaceRoot, ".boxteam"), { recursive: true });
  writeFileSync(path.join(workspaceRoot, ".boxteam/workspace-identity.json"), JSON.stringify({ workspace_id: WORKSPACE_ID }));
  const manager = new BrowserManager({ workspaceRoot, workspaceId: WORKSPACE_ID, ...options });
  manager.stateStore.write = writer;
  return manager;
}
function createOptions() {
  return { workspaceId: WORKSPACE_ID, browserId: `browser_${randomUUID().replaceAll("-", "")}`, ownerRevision: 1 };
}

describe("浏览器状态持久化", () => {
  test("重启时将带检查点的冻结浏览器恢复为可懒加载资源", async () => {
    const manager = managerWithWriter(async () => undefined);
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
    const manager = managerWithWriter(async () => undefined);
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
    const manager = managerWithWriter(async () => undefined);
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

  test("并发持久化写入严格串行", async () => {
    let active = 0;
    let maximumActive = 0;
    let writes = 0;
    const manager = managerWithWriter(async () => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      writes += 1;
      active -= 1;
    });

    await Promise.all([manager.persist(), manager.persist(), manager.persist()]);

    expect(writes).toBe(3);
    expect(maximumActive).toBe(1);
  });

  test("恢复后仅稳定资源 ID 可以定位浏览器，内部标签页 ID 明确拒绝", () => {
    const manager = managerWithWriter(async () => undefined);
    const session = {
      id: "browser_recovered",
      pageEntries: new Map([["page_recovered_internal", {}]]),
      record: { page_id: "page_recovered_internal" },
    };
    manager.sessions.set(session.id, session);

    expect(manager.get("browser_recovered")).toBe(session);
    expect(() => manager.get("page_recovered_internal")).toThrow("浏览器页面不存在");
  });

  test("传输 grant 只能消费一次，过期和撤销的版本均拒绝", async () => {
    const manager = managerWithWriter(async () => undefined);
    const browser = new BrowserSession({ manager, record: {
      browser_id: "browser_grant", workspace_id: WORKSPACE_ID,
      status: "running", transport_owner_revision: 1, reconciliation_status: "verified",
    } });
    manager.sessions.set(browser.id, browser);
    const arguments_ = { ownerRevision: 1, association: { session_id: "ses_owner", thread_id: "thr_owner" } };
    const single = manager.issueTransportGrant(browser.id, arguments_);
    expect(manager.consumeTransportGrant(browser.id, single.grant_token).association).toEqual(arguments_.association);
    expect(() => manager.consumeTransportGrant(browser.id, single.grant_token)).toThrow("已消费");
    const expired = manager.issueTransportGrant(browser.id, arguments_);
    manager.transportGrants.get(expired.grant_token).expiresAt = Date.now() - 1;
    expect(() => manager.consumeTransportGrant(browser.id, expired.grant_token)).toThrow("过期");
    const revoked = manager.issueTransportGrant(browser.id, arguments_);
    await manager.syncOwnerRevision(browser.id, 2);
    expect(() => manager.consumeTransportGrant(browser.id, revoked.grant_token)).toThrow("无效");
    expect(() => manager.issueTransportGrant(browser.id, arguments_)).toThrow("冲突");
    browser.record.reconciliation_status = "unknown";
    expect(() => manager.issueTransportGrant(browser.id, { ...arguments_, ownerRevision: 2 })).toThrow("尚未核实");
  });

  test("短时间内的后台写入请求合并为一次", async () => {
    let writes = 0;
    const manager = managerWithWriter(async () => {
      writes += 1;
    });

    manager.schedulePersist(5);
    manager.schedulePersist(5);
    manager.schedulePersist(5);
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(writes).toBe(1);
  });

  test("跨BrowserSession的检查点捕获严格串行", async () => {
    const manager = managerWithWriter(async () => undefined);
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
    const manager = managerWithWriter(async () => undefined);
    let closeCalls = 0;
    manager.sessions.set("browser_discarded", {
      status: "running",
      record: { resource_state: "discarded" },
      close: async () => { closeCalls += 1; },
      snapshot: () => ({
        browser_id: "browser_discarded",
        status: "running",
        resource_state: "discarded",
      }),
    });
    manager.runtimePool.shutdown = async () => undefined;

    await manager.shutdown();

    expect(closeCalls).toBe(0);
  });

  test("正常关闭管理器时将热浏览器转为可恢复检查点", async () => {
    const manager = managerWithWriter(async () => undefined);
    const shutdownReasons = [];
    manager.sessions.set("browser_running", {
      id: "browser_running",
      status: "running",
      record: { resource_state: "background" },
      checkpointForManagerShutdown: async (reason) => {
        shutdownReasons.push(reason);
      },
      markManagerShutdownCheckpointFailed: async () => {
        throw new Error("不应进入检查点失败分支");
      },
      snapshot: () => ({
        browser_id: "browser_running",
        status: "running",
        resource_state: "discarded",
      }),
    });
    manager.runtimePool.shutdown = async () => undefined;

    await manager.shutdown("browser_manager_sigterm");

    expect(shutdownReasons).toEqual(["browser_manager_sigterm"]);
  });

  test("工作区逻辑浏览器达到硬上限时明确拒绝新建", async () => {
    const manager = managerWithWriter(async () => undefined, {
      maxLogicalBrowsersPerWorkspace: 2,
    });
    manager.sessions.set("browser_a", { status: "running" });
    manager.sessions.set("browser_b", { status: "running" });

    await expect(manager.create(createOptions())).rejects.toMatchObject({
      code: "browser_workspace_logical_limit_exceeded",
    });
  });

  test("严重内存压力下暂停创建新浏览器", async () => {
    const manager = managerWithWriter(async () => undefined);
    manager.resourceGovernor = {
      snapshot: () => ({ last_sample: { level: "critical" } }),
    };

    await expect(manager.create(createOptions())).rejects.toMatchObject({
      code: "browser_creation_paused_memory_pressure",
    });
  });

  test("创建前为未生成检查点的资源预留工作区预算", async () => {
    const manager = managerWithWriter(async () => undefined, {
      checkpointReservationBytes: 4 * 1024 * 1024,
    });
    manager.stateStore.checkpointBudgetSnapshot = async () => ({
      used_bytes: 0,
      max_bytes: 6 * 1024 * 1024,
      remaining_bytes: 6 * 1024 * 1024,
    });
    manager.sessions.set("browser_uncheckpointed", {
            status: "running",
      record: { resource_state: "background", checkpoint: null },
    });

    await expect(manager.create(createOptions())).rejects.toMatchObject({
      code: "browser_checkpoint_workspace_quota_exceeded",
    });
  });

  test("并发创建不能绕过工作区逻辑资源硬上限", async () => {
    const manager = managerWithWriter(async () => undefined, {
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
    await expect(first).rejects.toThrow("结束并发创建测试");
  });
});
