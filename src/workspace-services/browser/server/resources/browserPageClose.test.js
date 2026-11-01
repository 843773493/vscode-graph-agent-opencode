import { EventEmitter } from "node:events";
import { expect, spyOn, test } from "bun:test";
import { BrowserSession } from "../browserSession.js";

async function pageCloseSession(stage) {
  const entered = Promise.withResolvers();
  const resume = Promise.withResolvers();
  const records = [];
  let stalled = false;
  const browser = new BrowserSession({
    manager: {
      persist: async () => {
        if (!stalled && (stage === "last_persist" && browser.record.status === "closed"
          || stage === "failure_persist" && browser.record.resource_transition_error)) {
          stalled = true;
          entered.resolve();
          await resume.promise;
        }
        records.push(browser.toRecord());
      },
      stateStore: { deleteCheckpoint: async () => undefined },
      runtimePool: { releaseContext: async () => undefined },
    },
    record: {
      browser_id: "browser_page_close_test", status: "running",
      workspace_id: "00000000-0000-4000-8000-000000000001",
      transport_owner_revision: 1, reconciliation_status: "verified",
    },
  });
  browser.browser = {};
  browser.context = { newCDPSession: async () => Object.assign(new EventEmitter(), { send: async () => undefined }) };
  browser.syncAndEmitState = async () => browser.snapshot();
  browser.pointerController.reset = async () => undefined;
  const makePage = () => Object.assign(new EventEmitter(), {
    url: () => "about:blank", title: async () => "page", isClosed: () => false,
  });
  if (stage !== "last_persist") await browser.registerPage(makePage(), { pageId: "replacement", activate: false });
  const closing = makePage();
  await browser.registerPage(closing, { pageId: "closing", activate: false });
  browser.activePageId = "closing";
  let firstReset = true;
  browser.pointerController.reset = async () => {
    if (!firstReset) return;
    firstReset = false;
    if (stage === "replacement") { entered.resolve(); await resume.promise; }
    if (stage === "failure_persist") throw new Error("受控替代页激活失败");
  };
  return { browser, closing, entered, resume, records };
}

test.each(["replacement", "failure_persist", "last_persist"])("关闭须等原页面close事件%s结算", async stage => {
  const { browser, closing, entered, resume, records } = await pageCloseSession(stage);
  const diagnostics = spyOn(process.stderr, "write").mockImplementation(() => true);
  try {
    closing.emit("close");
    await entered.promise;
    await expect(browser.close({ status: "deleted", operationDrainTimeoutMs: 20 })).rejects
      .toMatchObject({ code: "browser_close_operation_drain_timeout", statusCode: 408 });
    expect(browser.record.reconciliation_status).toBe("unknown");
    expect(records.some(row => row.status === "deleted" && row.reconciliation_status === "verified")).toBe(false);
    expect(browser.operationQueue.acceptedOperationExecutions.size).toBeGreaterThan(0);
    resume.resolve();
    expect(await browser.operationQueue.drain(100)).toBe(true);
    expect(browser.pageEntries.size).toBe(0);
    expect(browser.activePageId).toBeNull();
    await expect(browser.close({ status: "deleted" })).resolves
      .toMatchObject({ status: "deleted", reconciliation_status: "verified" });
    expect(records.at(-1)).toMatchObject({ status: "deleted", reconciliation_status: "verified" });
    if (stage === "failure_persist") expect(browser.record.resource_transition_error).toContain("reset_pointer");
  } finally {
    resume.resolve();
    await browser.operationQueue.drain(100);
    await browser.close({ status: "deleted" });
    diagnostics.mockRestore();
  }
});

test("Manager须等原close事件完成替代页激活后才采集检查点", async () => {
  const { browser, closing, entered, resume } = await pageCloseSession("replacement");
  const captures = [];
  browser.freeze = async () => { captures.push(browser.activePageId); };
  browser.discard = async () => undefined;
  try {
    closing.emit("close");
    await entered.promise;
    await expect(browser.checkpointForManagerShutdown("test", { operationDrainTimeoutMs: 20 })).rejects
      .toMatchObject({ code: "browser_shutdown_operation_drain_timeout" });
    expect(captures).toEqual([]);
    resume.resolve();
    expect(await browser.operationQueue.drain(100)).toBe(true);
    await browser.checkpointForManagerShutdown("test");
    expect(captures).toEqual(["replacement"]);
  } finally {
    resume.resolve();
    await browser.operationQueue.drain(100);
    await browser.close({ status: "deleted" });
  }
});

test("普通页面关闭仍激活原替代页，旧页面事件不删除新身份", async () => {
  const { browser, closing } = await pageCloseSession("normal");
  try {
    closing.emit("close");
    expect(await browser.operationQueue.drain(100)).toBe(true);
    expect(browser.activePageId).toBe("replacement");
    expect(browser.record.status).toBe("running");
    const entry = browser.pageEntries.get("replacement");
    await browser.handlePageClosed("replacement", closing);
    expect(browser.pageEntries.get("replacement")).toBe(entry);
  } finally {
    await browser.close({ status: "deleted" });
  }
});
