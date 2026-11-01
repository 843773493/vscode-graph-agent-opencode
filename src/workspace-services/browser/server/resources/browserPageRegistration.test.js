import { EventEmitter } from "node:events";
import { setImmediate } from "node:timers/promises";
import { expect, spyOn, test } from "bun:test";
import { BrowserSession } from "../browserSession.js";

function pendingPage(stage) {
  const entered = Promise.withResolvers();
  const resume = Promise.withResolvers();
  const persisted = [];
  const context = new EventEmitter();
  const page = Object.assign(new EventEmitter(), {
    url: () => "about:blank", title: async () => "popup", isClosed: () => false,
  });
  const cdp = Object.assign(new EventEmitter(), { send: async () => undefined });
  const browser = new BrowserSession({
    manager: {
      persist: async () => { persisted.push(browser.toRecord()); },
      stateStore: { deleteCheckpoint: async () => undefined },
      runtimePool: { releaseContext: async () => undefined },
    },
    record: {
      browser_id: "browser_registration_test", status: "running",
      workspace_id: "00000000-0000-4000-8000-000000000001",
      transport_owner_revision: 1, reconciliation_status: "verified",
    },
  });
  let firstPointerReset = true;
  const wait = async () => { entered.resolve(); await resume.promise; };
  context.newCDPSession = async () => { if (stage === "cdp") await wait(); return cdp; };
  browser.applyDeviceEmulation = async () => { if (stage === "device") await wait(); };
  browser.pointerController.reset = async () => {
    if (stage === "activation" && firstPointerReset) { firstPointerReset = false; await wait(); }
  };
  browser.deviceEmulationApplied = true;
  browser.browser = {};
  browser.context = context;
  browser.bindContextPageEvents();
  return { browser, context, page, entered, resume, persisted };
}

test.each(["cdp", "device", "activation"])("关闭排空已接纳的弹出页注册，迟到%s不能恢复页面", async (stage) => {
  const { browser, context, page, entered, resume, persisted } = pendingPage(stage);
  const diagnostics = spyOn(process.stderr, "write").mockImplementation(() => true);
  let registration;
  try {
    context.emit("page", page);
    registration = browser.pageRegistrationPromises.get(page).catch((error) => error);
    await entered.promise;
    const closing = browser.close({ status: "deleted", operationDrainTimeoutMs: 20 }).catch((error) => error);
    const outcome = await closing;
    expect(outcome).toMatchObject({ code: "browser_close_operation_drain_timeout", statusCode: 408 });
    expect(browser.record.reconciliation_status).toBe("unknown");
    expect(persisted.some((row) => row.status === "deleted" && row.reconciliation_status === "verified")).toBe(false);
    expect(browser.operationQueue.acceptedOperationExecutions.size).toBeGreaterThan(0);
    resume.resolve();
    const failure = await registration;
    expect(failure).toBeInstanceOf(Error);
    await setImmediate();
    expect(browser.pageEntries.size).toBe(0);
    expect(browser.activePageId).toBeNull();
    expect(browser.page).toBeNull();
    expect(browser.cdpSession).toBeNull();
    expect(await browser.operationQueue.drain(0)).toBe(true);
    await expect(browser.close({ status: "deleted" })).resolves.toMatchObject({ status: "deleted", reconciliation_status: "verified" });
    expect(persisted.at(-1)).toMatchObject({ status: "deleted", reconciliation_status: "verified" });
  } finally {
    resume.resolve();
    await registration;
    await browser.close({ status: "deleted" });
    diagnostics.mockRestore();
  }
});

test("旧context迟到的page事件不注册到新的context", async () => {
  const { browser, context, page, resume } = pendingPage("none");
  let registrations = 0;
  browser.context = { newCDPSession: async () => { registrations += 1; throw new Error("旧page不能进入新context"); } };
  const diagnostics = spyOn(process.stderr, "write").mockImplementation(() => true);
  try {
    context.emit("page", page);
    await setImmediate();
    expect(registrations).toBe(0);
    expect(browser.pageEntries.size).toBe(0);
    expect(browser.record.error_message).toBeUndefined();
  } finally {
    resume.resolve();
    await browser.operationQueue.drain(100);
    diagnostics.mockRestore();
  }
});

test("管理器停止新准入时，仍在场的原context注册可完成供检查点捕获", async () => {
  const { browser, context, page, entered, resume } = pendingPage("cdp");
  browser.syncAndEmitState = async () => browser.snapshot();
  let registration;
  try {
    context.emit("page", page);
    registration = browser.pageRegistrationPromises.get(page);
    await entered.promise;
    browser.closingRequested = true;
    resume.resolve();
    const entry = await registration;
    expect(browser.pageEntries.get(entry.pageId)).toBe(entry);
    expect(browser.page).toBe(page);
    expect(browser.context).toBe(context);
    expect(await browser.operationQueue.drain(100)).toBe(true);
  } finally {
    resume.resolve();
    await registration;
    await browser.close({ status: "deleted" });
  }
});
