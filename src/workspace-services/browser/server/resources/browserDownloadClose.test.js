import { EventEmitter } from "node:events";
import { setImmediate } from "node:timers/promises";
import { expect, spyOn, test } from "bun:test";
import { BrowserSession } from "../browserSession.js";

function downloadSession(stage) {
  const entered = Promise.withResolvers();
  const resume = Promise.withResolvers();
  const records = [];
  const saved = { download_id: "download_test", filename: "report.txt", status: "completed" };
  let stalled = false;
  const wait = async () => { entered.resolve(); await resume.promise; };
  const browser = new BrowserSession({
    manager: {
      writeDownload: async () => {
        if (stage === "save") await wait();
        if (stage === "error") throw new Error("受控下载拒绝");
        return saved;
      },
      persist: async () => {
        if (!stalled && (stage === "persist" && browser.record.downloads?.length
          || stage === "error" && browser.record.download_error)) {
          stalled = true;
          await wait();
        }
        records.push(browser.toRecord());
      },
      stateStore: { deleteCheckpoint: async () => undefined },
      runtimePool: { releaseContext: async () => undefined },
    },
    record: {
      browser_id: "browser_download_test", status: "running",
      workspace_id: "00000000-0000-4000-8000-000000000001",
      transport_owner_revision: 1, reconciliation_status: "verified",
    },
  });
  browser.browser = {};
  browser.context = {};
  browser.pointerController.reset = async () => undefined;
  return { browser, entered, resume, records, saved };
}

test.each(["save", "persist", "error"])("关闭须排空原下载%s，不提前核实终态", async stage => {
  const { browser, entered, resume, records, saved } = downloadSession(stage);
  const diagnostics = spyOn(process.stderr, "write").mockImplementation(() => true);
  const execution = browser.handleDownload({});
  try {
    await entered.promise;
    expect(browser.activeDownloads).toBe(1);
    await expect(browser.close({ status: "deleted", operationDrainTimeoutMs: 20 })).rejects
      .toMatchObject({ code: "browser_close_operation_drain_timeout", statusCode: 408 });
    expect(browser.record.reconciliation_status).toBe("unknown");
    expect(records.some(row => row.status === "deleted" && row.reconciliation_status === "verified")).toBe(false);
    expect(browser.operationQueue.acceptedOperationExecutions.size).toBeGreaterThan(0);
    resume.resolve();
    await execution;
    expect(browser.activeDownloads).toBe(0);
    if (stage === "error") expect(browser.record.download_error).toBe("受控下载拒绝");
    else expect(browser.record.downloads).toEqual([saved]);
    await expect(browser.close({ status: "deleted" })).resolves
      .toMatchObject({ status: "deleted", reconciliation_status: "verified" });
    expect(records.at(-1)).toMatchObject({ status: "deleted", reconciliation_status: "verified" });
  } finally {
    resume.resolve();
    await execution;
    await browser.close({ status: "deleted" });
    diagnostics.mockRestore();
  }
});

test("已释放页面的迟到download事件不得启动保存", async () => {
  const { browser, resume } = downloadSession("none");
  let downloads = 0;
  browser.manager.writeDownload = async () => { downloads += 1; throw new Error("旧页面不得保存下载"); };
  const page = Object.assign(new EventEmitter(), { url: () => "about:blank", title: async () => "download", isClosed: () => false });
  browser.context = { newCDPSession: async () => Object.assign(new EventEmitter(), { send: async () => undefined }) };
  const diagnostics = spyOn(process.stderr, "write").mockImplementation(() => true);
  try {
    await browser.registerPage(page, { activate: false });
    await browser.close({ status: "deleted" });
    page.emit("download", {});
    await setImmediate();
    expect(downloads).toBe(0);
    expect(browser.record.download_error).toBeUndefined();
    expect(browser.record.status).toBe("deleted");
  } finally {
    resume.resolve();
    await browser.operationQueue.drain(100);
    diagnostics.mockRestore();
  }
});

test("正常下载保留原记录并结清在途计数", async () => {
  const { browser, saved } = downloadSession("none");
  await browser.handleDownload({});
  expect(browser.record.downloads).toEqual([saved]);
  expect(browser.activeDownloads).toBe(0);
  expect(await browser.operationQueue.drain(0)).toBe(true);
  await expect(browser.close()).resolves.toMatchObject({ status: "closed", reconciliation_status: "verified" });
});
