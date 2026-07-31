import { chromium } from "playwright";
import { browserLaunchOptions } from "../browserRuntime.js";

const DEFAULT_IDLE_SHUTDOWN_MS = 30_000;

function scopedBrowserHandle(browser, context) {
  return Object.freeze({
    contexts: () => [context],
    isConnected: () => browser.isConnected(),
    version: () => browser.version(),
    close: async () => {
      throw new Error("当前 Browser 使用工作区共享 Chromium，请关闭 page 或 context，不能关闭共享 browser。");
    },
    newContext: async () => {
      throw new Error("当前 Browser 使用独立 context，请在当前 context 中创建 page。");
    },
  });
}

export class BrowserRuntimePool {
  constructor({
    launch = (options) => chromium.launch(options),
    launchOptions = browserLaunchOptions,
    idleShutdownMs = DEFAULT_IDLE_SHUTDOWN_MS,
    onDisconnect = () => undefined,
  } = {}) {
    this.launch = launch;
    this.launchOptions = launchOptions;
    this.idleShutdownMs = idleShutdownMs;
    this.onDisconnect = onDisconnect;
    this.browser = null;
    this.launchPromise = null;
    this.contexts = new Set();
    this.idleTimer = null;
    this.generation = 0;
    this.closing = false;
    this.expectedDisconnects = new WeakSet();
  }

  async ensureBrowser() {
    this.cancelIdleShutdown();
    if (this.browser?.isConnected()) return this.browser;
    if (!this.launchPromise) {
      this.launchPromise = this.launch(this.launchOptions()).then((browser) => {
        this.browser = browser;
        this.generation += 1;
        browser.once("disconnected", () => {
          const unexpected = !this.expectedDisconnects.has(browser);
          this.browser = null;
          this.contexts.clear();
          if (unexpected) this.onDisconnect(this.generation);
        });
        return browser;
      }).finally(() => {
        this.launchPromise = null;
      });
    }
    return await this.launchPromise;
  }

  async acquireContext(options) {
    const browser = await this.ensureBrowser();
    const context = await browser.newContext(options);
    this.contexts.add(context);
    return {
      browser,
      browserHandle: scopedBrowserHandle(browser, context),
      context,
      runtimeGeneration: this.generation,
    };
  }

  async releaseContext(context) {
    if (!context || !this.contexts.has(context)) return;
    this.contexts.delete(context);
    await context.close();
    if (this.contexts.size === 0) this.scheduleIdleShutdown();
  }

  scheduleIdleShutdown() {
    this.cancelIdleShutdown();
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null;
      void this.closeIdleBrowser();
    }, this.idleShutdownMs);
    this.idleTimer.unref?.();
  }

  cancelIdleShutdown() {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  async closeIdleBrowser() {
    if (this.contexts.size > 0 || !this.browser) return;
    const browser = this.browser;
    this.closing = true;
    this.expectedDisconnects.add(browser);
    try {
      await browser.close();
    } finally {
      this.closing = false;
      if (this.browser === browser) this.browser = null;
    }
  }

  async shutdown() {
    this.cancelIdleShutdown();
    this.closing = true;
    const browser = this.browser;
    this.contexts.clear();
    try {
      if (browser) {
        this.expectedDisconnects.add(browser);
        await browser.close();
      }
    } finally {
      this.browser = null;
      this.closing = false;
    }
  }

  snapshot() {
    return {
      connected: this.browser?.isConnected() === true,
      context_count: this.contexts.size,
      generation: this.generation,
    };
  }
}
