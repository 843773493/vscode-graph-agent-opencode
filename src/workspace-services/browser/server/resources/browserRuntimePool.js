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

/**
 * 从当前池取得的一份独立 context；关闭 context 仍经池释放，不关闭共享 Chromium。
 * @typedef {object} BrowserContextAcquisition
 * @property {import("playwright").Browser} browser 当前池共享的 Chromium 句柄。
 * @property {ReturnType<typeof scopedBrowserHandle>} browserHandle 限于所属 context 的受限句柄。
 * @property {import("playwright").BrowserContext} context 由调用方持有的独立 context。
 * @property {number} chromiumInstanceGeneration 当前池每次成功启动 Chromium 后递增的实例代际。
 * 同实例的 context 共用该值；池重建从零计数，不是持久 ID、授权版本或操作序号。
 */

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
    this.closePromise = null;
    this.pendingContextAcquisitions = 0;
    this.contexts = new Set();
    this.idleTimer = null;
    // 零表示尚未成功启动；重建池后重新计数，不从历史状态续接。
    this.chromiumInstanceGeneration = 0;
    this.expectedDisconnects = new WeakSet();
  }

  async ensureBrowser() {
    this.cancelIdleShutdown();
    if (this.closePromise) await this.closePromise;
    if (this.browser?.isConnected()) return this.browser;
    if (!this.launchPromise) {
      this.launchPromise = this.launch(this.launchOptions()).then((browser) => {
        this.browser = browser;
        const chromiumInstanceGeneration = ++this.chromiumInstanceGeneration;
        browser.once("disconnected", () => {
          const unexpected = !this.expectedDisconnects.has(browser);
          // 迟到事件只清理所属实例，通知沿创建时冻结的代际归属。
          if (this.browser === browser) {
            this.browser = null;
            this.contexts.clear();
          }
          if (unexpected) this.onDisconnect(chromiumInstanceGeneration);
        });
        return browser;
      }).finally(() => {
        this.launchPromise = null;
      });
    }
    return await this.launchPromise;
  }

  /** @returns {Promise<BrowserContextAcquisition>} */
  async acquireContext(options) {
    // 从请求进入起保留使用权，不能只统计已经创建完成的 context。
    this.pendingContextAcquisitions += 1;
    try {
      const browser = await this.ensureBrowser();
      if (this.closePromise || this.browser !== browser) {
        throw new Error("Browser运行时正在关闭，不能创建context");
      }
      const context = await browser.newContext(options);
      if (this.closePromise || this.browser !== browser) {
        await context.close();
        throw new Error("Browser运行时在context创建期间关闭");
      }
      this.contexts.add(context);
      return {
        browser,
        browserHandle: scopedBrowserHandle(browser, context),
        context,
        chromiumInstanceGeneration: this.chromiumInstanceGeneration,
      };
    } finally {
      this.pendingContextAcquisitions -= 1;
      this.scheduleIdleShutdown();
    }
  }

  async releaseContext(context) {
    if (!context || !this.contexts.has(context)) return;
    await context.close();
    this.contexts.delete(context);
    if (this.contexts.size === 0) this.scheduleIdleShutdown();
  }

  scheduleIdleShutdown() {
    this.cancelIdleShutdown();
    if (this.contexts.size > 0 || this.pendingContextAcquisitions > 0 || this.closePromise || !this.browser) return;
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
    if (this.contexts.size > 0 || this.pendingContextAcquisitions > 0) return;
    await this.shutdown();
  }

  async shutdown() {
    this.cancelIdleShutdown();
    if (this.closePromise) return await this.closePromise;
    const launching = this.launchPromise;
    this.closePromise = (async () => {
      // 关闭请求覆盖已经接纳的启动，不能在Chromium句柄尚未返回时提前完成。
      const browser = launching ? await launching : this.browser;
      if (!browser) return;
      this.expectedDisconnects.add(browser);
      try {
        await browser.close();
      } catch (error) {
        this.expectedDisconnects.delete(browser);
        throw error;
      }
      // 关闭成功或所属实例已断线，才能释放其句柄和context归属。
      if (this.browser === browser) {
        this.browser = null;
        this.contexts.clear();
      }
    })().finally(() => {
      this.closePromise = null;
    });
    await this.closePromise;
  }

  snapshot() {
    return {
      connected: this.browser?.isConnected() === true,
      context_count: this.contexts.size,
      generation: this.chromiumInstanceGeneration,
    };
  }
}
