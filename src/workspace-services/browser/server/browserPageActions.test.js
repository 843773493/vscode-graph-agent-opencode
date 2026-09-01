import { describe, expect, test } from "bun:test";
import {
  clickElement,
  handleDialog,
  runPlaywrightCode,
  runWithStaleSelectorRecovery,
} from "./browserPageActions.js";
import { withTimeout } from "./browserRuntime.js";

describe("用户处理浏览器文件选择", () => {
  test("将用户设备文件以内存载荷交给 Playwright", async () => {
    let selectedFiles = null;
    const session = {
      pendingDialog: null,
      pendingFileChooser: {
        setFiles: async (files) => {
          selectedFiles = files;
        },
      },
    };

    const result = await handleDialog(session, {
      filePayloads: [{
        name: "hello.txt",
        mimeType: "text/plain",
        data: Buffer.from("你好").toString("base64"),
      }],
    });

    expect(result.summary).toContain("1 个文件");
    expect(selectedFiles[0].name).toBe("hello.txt");
    expect(selectedFiles[0].buffer.toString("utf8")).toBe("你好");
    expect(session.pendingFileChooser).toBeNull();
  });
});

describe("失效元素 ref", () => {
  test("目标已移除时立即返回专用错误", async () => {
    const locator = {
      count: async () => 0,
      click: async () => {
        throw new Error("不应执行 click");
      },
    };
    const page = { locator: () => ({ first: () => locator }) };
    const refs = new Map([["r1_e1", { selector: "[data-boxteam-ref=r1_e1]" }]]);

    try {
      await clickElement(page, refs, { ref: "r1_e1" });
      throw new Error("预期 clickElement 失败");
    } catch (error) {
      expect(error.code).toBe("browser_stale_element_ref");
      expect(error.message).toContain("请重新调用 readPage");
    }
  });

  test("导航后显式 selector 短暂缺失时等待并完成点击", async () => {
    let available = false;
    let clicked = false;
    const locator = {
      count: async () => (available ? 1 : 0),
      click: async () => {
        clicked = true;
      },
    };
    const page = {
      locator: () => ({ first: () => locator }),
      waitForSelector: async (selector, options) => {
        expect(selector).toBe("#game");
        expect(options).toMatchObject({ state: "attached" });
        available = true;
      },
    };

    await clickElement(page, new Map(), { selector: "#game" });

    expect(clicked).toBe(true);
  });

  test("selector stale 时只重新读取一次并重新定位，仍失败则停止", async () => {
    let actionCount = 0;
    let refreshCount = 0;
    const stale = Object.assign(new Error("stale"), { code: "browser_stale_element_ref" });
    const result = await runWithStaleSelectorRecovery(
      {},
      new Map(),
      { selector: "#game" },
      async () => {
        actionCount += 1;
        if (actionCount === 1) throw stale;
        return "clicked";
      },
      async () => {
        refreshCount += 1;
      },
    );

    expect(result).toBe("clicked");
    expect(actionCount).toBe(2);
    expect(refreshCount).toBe(1);
  });
});

describe("Playwright 代码超时", () => {
  test("用户脚本的页面 action 超时预算短于工具预算", async () => {
    const timeouts = [];
    const page = {
      setDefaultTimeout: (timeoutMs) => timeouts.push(["action", timeoutMs]),
      setDefaultNavigationTimeout: (timeoutMs) => timeouts.push(["navigation", timeoutMs]),
    };

    const result = await runPlaywrightCode(
      { page, context: {}, browser: {} },
      { code: "return 'ok';", timeoutMs: 1000 },
    );

    expect(result.result).toBe("ok");
    expect(timeouts).toEqual([
      ["action", 750],
      ["navigation", 750],
    ]);
  });

  test("触发取消信号并返回可重试的超时错误", async () => {
    let timeoutError = null;
    const run = runPlaywrightCode(
      { page: {}, context: {}, browser: {} },
      {
        code: "await new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason)));",
        timeoutMs: 10,
      },
      {
        onTimeout: (error) => {
          timeoutError = error;
        },
      },
    );

    await expect(run).rejects.toMatchObject({
      code: "browser_tool_timeout",
      timeout_ms: 10,
      retryable: true,
      recovery: "page_reset",
    });
    expect(timeoutError).toMatchObject({ code: "browser_tool_timeout" });
  });

  test("超时不等待页面 reset，恢复 promise 由调用方单独收敛", async () => {
    let recoveryFinished = false;
    let finishRecovery;
    const recovery = new Promise((resolve) => {
      finishRecovery = () => {
        recoveryFinished = true;
        resolve();
      };
    });

    await expect(
      withTimeout(new Promise(() => undefined), 5, "Playwright 代码执行", {
        onTimeout: () => recovery,
      }),
    ).rejects.toMatchObject({
      code: "browser_tool_timeout",
      retryable: true,
    });

    expect(recoveryFinished).toBe(false);
    finishRecovery();
    await recovery;
    expect(recoveryFinished).toBe(true);
  });
});
