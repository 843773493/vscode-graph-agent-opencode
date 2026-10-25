import { expect, test } from "bun:test";
import { BrowserSession } from "../browserSession.js";

function pendingScript() {
  let release;
  let complete;
  const finished = new Promise((resolve) => { complete = resolve; });
  const context = {
    pending: new Promise((resolve) => { release = resolve; }),
    complete,
    started: false,
  };
  const saved = [];
  let closes = 0;
  const browser = new BrowserSession({
    manager: {
      persist: async () => { saved.push(browser.toRecord()); },
      stateStore: { deleteCheckpoint: async () => undefined },
      runtimePool: { releaseContext: async (released) => {
        expect(released).toBe(context);
        closes += 1;
      } },
    },
    record: {
      browser_id: "browser_close_execution", workspace_id: "00000000-0000-4000-8000-000000000001",
      transport_owner_revision: 1, reconciliation_status: "verified", status: "running",
    },
  });
  browser.browser = {};
  browser.context = context;
  browser.page = {};
  browser.cdpSession = {};
  // 页面重置由其它真实Chromium场景验证，此处只控制原脚本何时真正结束。
  browser.recoverAfterPlaywrightTimeout = async () => undefined;
  return { browser, context, saved, release, finished, closes: () => closes };
}

for (const status of ["closed", "deleted"]) {
  test.each([false, true])(`${status}等待超时后的原脚本结束，再发布终态：reject=%s`, async (reject) => {
    const { browser, context, saved, release, finished, closes } = pendingScript();
    context.reject = reject;
    try {
      await expect(browser.runPlaywrightCode({
        code: "context.started = true; await context.pending; context.complete(); if (context.reject) throw new Error('原脚本迟到失败'); return 'late';",
        timeoutMs: 5,
      })).rejects.toMatchObject({ code: "browser_tool_timeout", recovery: "page_reset" });
      expect(await browser.operationQueue.drain(0)).toBe(false);
      await expect(browser.close({ status, operationDrainTimeoutMs: 10 }))
        .rejects.toMatchObject({ code: "browser_close_operation_drain_timeout", statusCode: 408 });
      expect(browser.record.status).toBe("running");
      expect(browser.record.reconciliation_status).toBe("unknown");
      expect(saved.at(-1).status).toBe("running");
      expect(saved.at(-1).reconciliation_status).toBe("unknown");
      expect(closes()).toBe(1);
      expect(await browser.operationQueue.drain(0)).toBe(false);
      release();
      await finished;
      expect(await browser.operationQueue.drain()).toBe(true);
      await expect(browser.close({ status })).resolves.toMatchObject({ status, reconciliation_status: "verified" });
      expect(closes()).toBe(1);
      expect(saved.at(-1).status).toBe(status);
      expect(saved.at(-1).reconciliation_status).toBe("verified");
    } finally {
      release();
      if (context.started) await finished;
    }
  });
}
