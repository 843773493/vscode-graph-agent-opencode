import { inspect } from "node:util";
import { describe, expect, spyOn, test } from "bun:test";
import { BrowserSession } from "../browserSession.js";
import { captureBrowserCheckpoint, checkpointRestoreUrl, restoreBrowserCheckpoint } from "./browserCheckpoint.js";

describe("浏览器检查点的运行时失败", () => {
  test.each(["capture_page", "capture_context", "acquire_context", "create_page", "restore_navigation", "restore_controls"])(
    "%s 保留原始异常与失败阶段，不复制原生存储位置", async (stage) => {
      const cause = Object.assign(new Error("EACCES /private/browser/profile"), { code: "EACCES" });
      const fail = async () => { throw cause; };
      const logged = spyOn(process.stderr, "write").mockImplementation(() => undefined);
      const browser = new BrowserSession({ manager: { persist: async () => undefined }, record: {
        browser_id: "browser_checkpoint_error", status: "running", reconciliation_status: "verified",
      } });
      const page = {
        isClosed: () => false, title: async () => "页面", url: () => "file:///user/opened.html",
        evaluate: stage === "capture_page" || stage === "restore_controls" ? fail : async () => ({}),
        goto: stage === "restore_navigation" ? fail : async () => undefined,
      };
      browser.context = {
        storageState: stage === "capture_context" ? fail : async () => ({}),
        newPage: stage === "create_page" ? fail : async () => page,
      };
      const entry = { pageId: "page_checkpoint", page };
      browser.pageEntries.set(entry.pageId, entry);
      browser.registerPage = async () => entry;
      let releases = 0;
      browser.manager.runtimePool = {
        acquireContext: stage === "acquire_context" ? fail : async () => ({ browser: {}, context: browser.context }),
        releaseContext: async () => { releases += 1; },
      };
      try {
        const operation = stage.startsWith("capture_") ? captureBrowserCheckpoint(browser) : restoreBrowserCheckpoint(browser, {
          version: 1, browser_id: browser.id, pages: [{ page_id: entry.pageId, url: page.url() }],
        });
        const error = await operation.catch((failure) => failure);
        expect(error.cause).toBe(cause);
        expect(error.message).toContain(`stage=${stage}`);
        expect(error.message).toContain(browser.id);
        expect(error.message).not.toContain("/private/browser/profile");
        expect(error.code).toBe(stage.startsWith("restore_") ? "browser_checkpoint_page_restore_failed" : "EACCES");
        if (stage.startsWith("restore_")) expect(error.message).toContain(page.url());
        expect(logged.mock.calls.some((args) => args[0].includes(inspect(cause)))).toBe(true);
        expect(releases).toBe(stage === "create_page" || stage.startsWith("restore_") ? 1 : 0);
      } finally { logged.mockRestore(); }
    },
  );
});

describe("浏览器检查点恢复地址", () => {
  test("普通SPA优先恢复pushState后的实际地址", () => {
    expect(checkpointRestoreUrl({
      url: "https://example.test/app/current-route",
      requested_url: "https://example.test/app",
      navigation_error: null,
    })).toBe("https://example.test/app/current-route");
  });

  test("Chromium错误页改用用户请求地址重试", () => {
    expect(checkpointRestoreUrl({
      url: "chrome-error://chromewebdata/",
      requested_url: "https://unreachable.example.test/",
      navigation_error: { message: "ERR_CONNECTION_REFUSED" },
    })).toBe("https://unreachable.example.test/");
  });
});
