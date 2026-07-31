import { describe, expect, test } from "bun:test";
import { BrowserManager } from "./browserManager.js";

function managerWithWriter(writer) {
  const manager = new BrowserManager({
    workspaceRoot: process.cwd(),
    browserFrontendBaseUrl: "http://browser.test",
  });
  manager.stateStore.write = writer;
  return manager;
}

describe("浏览器状态持久化", () => {
  test("重启时将带检查点的冻结浏览器恢复为可懒加载资源", async () => {
    const manager = managerWithWriter(async () => undefined);
    manager.stateStore.readRecords = async () => [{
      browser_id: "browser_recoverable",
      session_id: "session_recoverable",
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
      session_id: "session_missing_checkpoint",
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

  test("单会话逻辑浏览器达到硬上限时明确拒绝新建", async () => {
    const manager = new BrowserManager({
      workspaceRoot: process.cwd(),
      browserFrontendBaseUrl: "http://browser.test",
      maxLogicalBrowsersPerSession: 2,
    });
    manager.sessions.set("browser_a", { sessionId: "session_limit", status: "running" });
    manager.sessions.set("browser_b", { sessionId: "session_limit", status: "running" });

    await expect(manager.create({ sessionId: "session_limit" })).rejects.toMatchObject({
      code: "browser_session_logical_limit_exceeded",
    });
  });

  test("严重内存压力下暂停创建新浏览器", async () => {
    const manager = managerWithWriter(async () => undefined);
    manager.resourceGovernor = {
      snapshot: () => ({ last_sample: { level: "critical" } }),
    };

    await expect(manager.create({ sessionId: "session_pressure" })).rejects.toMatchObject({
      code: "browser_creation_paused_memory_pressure",
    });
  });

  test("创建前为未生成检查点的资源预留工作区预算", async () => {
    const manager = new BrowserManager({
      workspaceRoot: process.cwd(),
      browserFrontendBaseUrl: "http://browser.test",
      checkpointReservationBytes: 4 * 1024 * 1024,
    });
    manager.stateStore.checkpointBudgetSnapshot = async () => ({
      used_bytes: 0,
      max_bytes: 6 * 1024 * 1024,
      remaining_bytes: 6 * 1024 * 1024,
    });
    manager.sessions.set("browser_uncheckpointed", {
      sessionId: "another_session",
      status: "running",
      record: { resource_state: "background", checkpoint: null },
    });

    await expect(manager.create({ sessionId: "session_budget" })).rejects.toMatchObject({
      code: "browser_checkpoint_workspace_quota_exceeded",
    });
  });

  test("并发创建不能绕过单会话逻辑资源硬上限", async () => {
    const manager = new BrowserManager({
      workspaceRoot: process.cwd(),
      browserFrontendBaseUrl: "http://browser.test",
      maxLogicalBrowsersPerSession: 1,
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

    const first = manager.create({ sessionId: "session_concurrent" });
    while (!rejectFirstStart) await Promise.resolve();
    const second = manager.create({ sessionId: "session_concurrent" });

    await expect(second).rejects.toMatchObject({ code: "browser_session_logical_limit_exceeded" });
    rejectFirstStart(new Error("结束并发创建测试"));
    await expect(first).rejects.toThrow("结束并发创建测试");
  });
});
