import path from "node:path";
import { chromium } from "playwright";

export class WebPlaywrightDriver {
  #browser;
  #context;
  #page;

  constructor(runContext) {
    this.runContext = runContext;
  }

  get page() {
    if (!this.#page) throw new Error("Web Playwright driver 尚未启动");
    return this.#page;
  }

  async launch({ baseUrl, executablePath, viewport } = {}) {
    if (!baseUrl) throw new Error("启动 Web Playwright driver 必须提供 baseUrl");
    this.baseUrl = new URL(baseUrl).toString();
    this.#browser = await chromium.launch({
      executablePath: executablePath || undefined,
      headless: true,
    });
    this.#context = await this.#browser.newContext({
      viewport: viewport ?? { width: 1440, height: 900 },
    });
    await this.#context.tracing.start({ screenshots: true, snapshots: true });
    this.#page = await this.#context.newPage();
    this.runContext.addCleanup("Web Playwright driver", () => this.close());
    return this;
  }

  async open(pathname = "/") {
    const target = new URL(pathname, this.baseUrl);
    await this.page.goto(target.toString(), {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
  }

  async observeResponse(pattern, action) {
    const responsePromise = this.page.waitForResponse(pattern);
    await action();
    return responsePromise;
  }

  async screenshot(name) {
    const outputPath = path.join(this.runContext.artifactsDir, name);
    await this.page.screenshot({ path: outputPath, fullPage: true });
    return outputPath;
  }

  async close() {
    const context = this.#context;
    const browser = this.#browser;
    this.#page = undefined;
    this.#context = undefined;
    this.#browser = undefined;
    if (context) {
      await context.tracing.stop({
        path: path.join(this.runContext.artifactsDir, "playwright-trace.zip"),
      });
      await context.close();
    }
    if (browser) await browser.close();
  }
}
