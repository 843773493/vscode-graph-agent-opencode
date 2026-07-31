import { EventEmitter } from "node:events";
import { describe, expect, test } from "bun:test";
import { browserLaunchOptions } from "../browserRuntime.js";
import { BrowserRuntimePool } from "./browserRuntimePool.js";

class FakeBrowser extends EventEmitter {
  constructor() {
    super();
    this.connected = true;
    this.contextsCreated = [];
  }

  isConnected() { return this.connected; }
  version() { return "test"; }

  async newContext(options) {
    const context = {
      options,
      closed: false,
      close: async () => { context.closed = true; },
    };
    this.contextsCreated.push(context);
    return context;
  }

  async close() {
    this.connected = false;
    this.emit("disconnected");
  }
}

describe("工作区共享 Chromium 运行时", () => {
  test("由 Browser Manager 独占进程信号处理以便先生成检查点", () => {
    expect(browserLaunchOptions()).toMatchObject({
      handleSIGINT: false,
      handleSIGTERM: false,
      handleSIGHUP: false,
    });
  });

  test("多个 Browser 资源复用一个 browser 且 context 隔离", async () => {
    let launches = 0;
    const browser = new FakeBrowser();
    const pool = new BrowserRuntimePool({
      launch: async () => { launches += 1; return browser; },
      idleShutdownMs: 60_000,
    });

    const first = await pool.acquireContext({ storageState: { cookies: [], origins: [] } });
    const second = await pool.acquireContext({ storageState: { cookies: [], origins: [] } });

    expect(launches).toBe(1);
    expect(first.browser).toBe(second.browser);
    expect(first.context).not.toBe(second.context);
    expect(pool.snapshot().context_count).toBe(2);
    await expect(first.browserHandle.close()).rejects.toThrow("共享 Chromium");
    await pool.releaseContext(first.context);
    expect(second.context.closed).toBe(false);
    await pool.shutdown();
  });

  test("正常关闭后的延迟 disconnected 事件不误报运行时崩溃", async () => {
    let disconnects = 0;
    const browser = new FakeBrowser();
    browser.close = async () => {
      browser.connected = false;
      setTimeout(() => browser.emit("disconnected"), 0);
    };
    const pool = new BrowserRuntimePool({
      launch: async () => browser,
      onDisconnect: () => { disconnects += 1; },
    });
    await pool.acquireContext({});

    await pool.shutdown();
    await new Promise((resolve) => setTimeout(resolve, 5));

    expect(disconnects).toBe(0);
  });
});
