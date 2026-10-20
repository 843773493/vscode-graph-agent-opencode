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

  test("关闭失败保留仍存活的运行时和context，仍能释放context并重试关闭", async () => {
    const browser = new FakeBrowser();
    const failure = new Error("Chromium关闭被拒绝");
    const close = browser.close.bind(browser);
    const pool = new BrowserRuntimePool({ launch: async () => browser });
    const first = await pool.acquireContext({});
    browser.close = async () => { throw failure; };
    try {
      await expect(pool.shutdown()).rejects.toBe(failure);
      expect(browser.isConnected()).toBe(true);
      expect(pool.snapshot()).toEqual({ connected: true, context_count: 1, generation: 1 });
      expect(pool.closePromise).toBeNull();
      await pool.releaseContext(first.context);
      expect(first.context.closed).toBe(true);
      expect(pool.snapshot().context_count).toBe(0);
      browser.close = close;
      await pool.shutdown();
      expect(browser.isConnected()).toBe(false);
      expect(pool.snapshot()).toEqual({ connected: false, context_count: 0, generation: 1 });
    } finally {
      browser.close = close;
      await pool.shutdown();
      await close();
    }
  });

  test("关闭失败后的真实断线仍通知所属代际，不继续当作预期关闭", async () => {
    const browser = new FakeBrowser();
    const failure = new Error("Chromium关闭失败");
    const close = browser.close.bind(browser);
    const disconnects = [];
    const pool = new BrowserRuntimePool({
      launch: async () => browser,
      onDisconnect: (chromiumInstanceGeneration) => disconnects.push(chromiumInstanceGeneration),
    });
    await pool.acquireContext({});
    browser.close = async () => { throw failure; };
    try {
      await expect(pool.shutdown()).rejects.toBe(failure);
      browser.connected = false;
      browser.emit("disconnected");
      expect(disconnects).toEqual([1]);
      expect(pool.snapshot()).toEqual({ connected: false, context_count: 0, generation: 1 });
    } finally {
      browser.close = close;
      await pool.shutdown();
      await close();
    }
  });

  test("关闭已断线但Promise随后失败时，不恢复已经结束的运行时", async () => {
    const browser = new FakeBrowser();
    const failure = new Error("关闭回执失败");
    const close = browser.close.bind(browser);
    const disconnects = [];
    const pool = new BrowserRuntimePool({
      launch: async () => browser,
      onDisconnect: (chromiumInstanceGeneration) => disconnects.push(chromiumInstanceGeneration),
    });
    await pool.acquireContext({});
    browser.close = async () => { await close(); throw failure; };
    try {
      await expect(pool.shutdown()).rejects.toBe(failure);
      expect(disconnects).toEqual([]);
      expect(pool.snapshot()).toEqual({ connected: false, context_count: 0, generation: 1 });
      expect(pool.closePromise).toBeNull();
    } finally {
      browser.close = close;
      await pool.shutdown();
    }
  });

  test("旧实例正常关闭后的迟到断线不清空新实例和context", async () => {
    const oldBrowser = new FakeBrowser();
    const currentBrowser = new FakeBrowser();
    const browsers = [oldBrowser, currentBrowser];
    const disconnects = [];
    let launches = 0;
    oldBrowser.close = async () => { oldBrowser.connected = false; };
    const pool = new BrowserRuntimePool({
      launch: async () => { launches += 1; return browsers.shift(); },
      onDisconnect: (chromiumInstanceGeneration) => disconnects.push(chromiumInstanceGeneration),
    });
    try {
      const first = await pool.acquireContext({});
      await pool.releaseContext(first.context);
      await pool.closeIdleBrowser();
      const current = await pool.acquireContext({});

      oldBrowser.emit("disconnected");

      expect(pool.snapshot()).toEqual({ connected: true, context_count: 1, generation: 2 });
      expect(current.context.closed).toBe(false);
      const next = await pool.acquireContext({});
      expect(next.browser).toBe(currentBrowser);
      expect(next.chromiumInstanceGeneration).toBe(2);
      expect(launches).toBe(2);
      expect(disconnects).toEqual([]);
    } finally {
      await pool.shutdown();
    }
  });

  test("旧实例异常断线只通知所属代际，不把新实例标成断线", async () => {
    const oldBrowser = new FakeBrowser();
    const currentBrowser = new FakeBrowser();
    const browsers = [oldBrowser, currentBrowser];
    const disconnects = [];
    const pool = new BrowserRuntimePool({
      launch: async () => browsers.shift(),
      onDisconnect: (chromiumInstanceGeneration) => disconnects.push(chromiumInstanceGeneration),
    });
    try {
      const first = await pool.acquireContext({});
      await pool.releaseContext(first.context);
      oldBrowser.connected = false;
      const current = await pool.acquireContext({});

      oldBrowser.emit("disconnected");

      expect(disconnects).toEqual([1]);
      expect(pool.snapshot()).toEqual({ connected: true, context_count: 1, generation: 2 });
      expect(current.context.closed).toBe(false);

      currentBrowser.connected = false;
      currentBrowser.emit("disconnected");
      expect(disconnects).toEqual([1, 2]);
      expect(pool.snapshot()).toEqual({ connected: false, context_count: 0, generation: 2 });
    } finally {
      await pool.shutdown();
    }
  });

  test("另一个context正在创建时，释放最后一个已有context不能关闭运行时", async () => {
    const browser = new FakeBrowser();
    const pool = new BrowserRuntimePool({ launch: async () => browser });
    const first = await pool.acquireContext({});
    const creating = Promise.withResolvers();
    const resumeCreation = Promise.withResolvers();
    const newContext = browser.newContext.bind(browser);
    browser.newContext = async (options) => {
      creating.resolve();
      await resumeCreation.promise;
      if (!browser.isConnected()) throw new Error("context创建期间运行时被关闭");
      return await newContext(options);
    };
    const acquiring = pool.acquireContext({});
    try {
      await creating.promise;
      await pool.releaseContext(first.context);
      await pool.closeIdleBrowser();
      expect(browser.isConnected()).toBe(true);
      resumeCreation.resolve();
      const second = await acquiring;
      expect(second.browser).toBe(browser);
      expect(pool.snapshot().context_count).toBe(1);
    } finally {
      resumeCreation.resolve();
      await acquiring.catch(() => undefined);
      await pool.shutdown();
    }
  });

  test("空闲运行时已经开始关闭时，新context等待关闭完成再使用新实例", async () => {
    const oldBrowser = new FakeBrowser();
    const currentBrowser = new FakeBrowser();
    const browsers = [oldBrowser, currentBrowser];
    const finishClose = Promise.withResolvers();
    const close = oldBrowser.close.bind(oldBrowser);
    oldBrowser.close = async () => {
      await finishClose.promise;
      await close();
    };
    const pool = new BrowserRuntimePool({ launch: async () => browsers.shift() });
    const first = await pool.acquireContext({});
    await pool.releaseContext(first.context);
    const closing = pool.closeIdleBrowser();
    const acquiring = pool.acquireContext({});
    try {
      finishClose.resolve();
      await closing;
      const current = await acquiring;
      expect(current.browser).toBe(currentBrowser);
      expect(current.chromiumInstanceGeneration).toBe(2);
      expect(oldBrowser.contextsCreated).toHaveLength(1);
      expect(pool.snapshot()).toEqual({ connected: true, context_count: 1, generation: 2 });
    } finally {
      finishClose.resolve();
      await Promise.allSettled([closing, acquiring]);
      await pool.shutdown();
    }
  });

  test("启动中收到关闭请求，等待该实例关闭且不再向旧请求交付context", async () => {
    const browser = new FakeBrowser();
    const nextBrowser = new FakeBrowser();
    const launch = Promise.withResolvers();
    const close = browser.close.bind(browser);
    let launches = 0;
    let closes = 0;
    browser.close = async () => { closes += 1; await close(); };
    const pool = new BrowserRuntimePool({
      launch: async () => ++launches === 1 ? await launch.promise : nextBrowser,
    });
    const acquiring = pool.acquireContext({}).catch((error) => error);
    let closed = false;
    const closing = pool.shutdown().then(() => { closed = true; });
    const secondClosing = pool.shutdown();
    try {
      await Promise.resolve();
      expect(closed).toBe(false);
      launch.resolve(browser);
      await Promise.all([closing, secondClosing]);
      expect(await acquiring).toBeInstanceOf(Error);
      expect(browser.isConnected()).toBe(false);
      expect(browser.contextsCreated).toHaveLength(0);
      expect(closes).toBe(1);
      expect(pool.snapshot()).toEqual({ connected: false, context_count: 0, generation: 1 });
      const next = await pool.acquireContext({});
      expect(next.browser).toBe(nextBrowser);
      expect(next.chromiumInstanceGeneration).toBe(2);
    } finally {
      launch.resolve(browser);
      await Promise.allSettled([acquiring, closing, secondClosing]);
      await pool.shutdown();
      await close();
    }
  });

  test("关闭后迟到的context被释放，不混入下一代运行时", async () => {
    const browser = new FakeBrowser();
    const nextBrowser = new FakeBrowser();
    const browsers = [browser, nextBrowser];
    const creating = Promise.withResolvers();
    const resume = Promise.withResolvers();
    const newContext = browser.newContext.bind(browser);
    browser.newContext = async (options) => {
      creating.resolve();
      await resume.promise;
      return await newContext(options);
    };
    const pool = new BrowserRuntimePool({ launch: async () => browsers.shift() });
    const acquiring = pool.acquireContext({}).catch((error) => error);
    try {
      await creating.promise;
      await pool.shutdown();
      const next = await pool.acquireContext({});
      resume.resolve();
      expect(await acquiring).toBeInstanceOf(Error);
      expect(browser.contextsCreated[0].closed).toBe(true);
      expect(next.browser).toBe(nextBrowser);
      expect(next.context.closed).toBe(false);
      expect(pool.snapshot()).toEqual({ connected: true, context_count: 1, generation: 2 });
      expect(pool.pendingContextAcquisitions).toBe(0);
    } finally {
      resume.resolve();
      await acquiring;
      await pool.shutdown();
      await browser.close();
    }
  });

  test("启动失败同时结束获取与关闭请求，保留原始异常且允许后续正常启动", async () => {
    const failure = new Error("Chromium启动失败");
    const launch = Promise.withResolvers();
    const browser = new FakeBrowser();
    let launches = 0;
    const pool = new BrowserRuntimePool({
      launch: async () => ++launches === 1 ? await launch.promise : browser,
    });
    const acquiring = pool.acquireContext({}).catch((error) => error);
    const closing = pool.shutdown().catch((error) => error);
    try {
      launch.reject(failure);
      expect(await acquiring).toBe(failure);
      expect(await closing).toBe(failure);
      expect(pool.launchPromise).toBeNull();
      expect(pool.closePromise).toBeNull();
      expect(pool.pendingContextAcquisitions).toBe(0);
      expect(pool.snapshot()).toEqual({ connected: false, context_count: 0, generation: 0 });
      const next = await pool.acquireContext({});
      expect(next.browser).toBe(browser);
      expect(next.chromiumInstanceGeneration).toBe(1);
    } finally {
      launch.reject(failure);
      await Promise.allSettled([acquiring, closing]);
      await pool.shutdown();
      await browser.close();
    }
  });

  test("空闲关闭与manager关闭交错时，只关闭一次同一运行时", async () => {
    const browser = new FakeBrowser();
    const finishClose = Promise.withResolvers();
    const close = browser.close.bind(browser);
    let closes = 0;
    browser.close = async () => {
      closes += 1;
      await finishClose.promise;
      await close();
    };
    const pool = new BrowserRuntimePool({ launch: async () => browser });
    const first = await pool.acquireContext({});
    await pool.releaseContext(first.context);
    const closing = pool.closeIdleBrowser();
    const shutdown = pool.shutdown();
    finishClose.resolve();
    await Promise.all([closing, shutdown]);
    expect(closes).toBe(1);
    expect(pool.snapshot()).toEqual({ connected: false, context_count: 0, generation: 1 });
  });

  test("创建context失败后，没有其它使用者的运行时仍会按空闲策略关闭", async () => {
    const browser = new FakeBrowser();
    const failure = new Error("context创建失败");
    browser.newContext = async () => { throw failure; };
    const disconnected = Promise.withResolvers();
    browser.once("disconnected", disconnected.resolve);
    const pool = new BrowserRuntimePool({ launch: async () => browser, idleShutdownMs: 0 });
    let watchdog;
    try {
      await expect(pool.acquireContext({})).rejects.toBe(failure);
      await Promise.race([
        disconnected.promise,
        new Promise((_, reject) => {
          watchdog = setTimeout(() => reject(new Error("失败后未回收空闲运行时")), 100);
        }),
      ]);
      expect(browser.isConnected()).toBe(false);
    } finally {
      clearTimeout(watchdog);
      await pool.shutdown();
    }
  });
});
