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
    await this.#page.goto(this.baseUrl, {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    const guestAccess = await this.#page.evaluate(async () => {
      const credentialResponse = await fetch("/api/gateway/auth/local-credential");
      if (!credentialResponse.ok) {
        throw new Error(`获取 Gateway 本地凭据失败: HTTP ${credentialResponse.status}`);
      }
      const credentialPayload = await credentialResponse.json();
      const token = credentialPayload?.data?.token;
      if (typeof token !== "string" || token.length === 0) {
        throw new Error("Gateway 本地凭据响应缺少 token");
      }
      const response = await fetch("/api/gateway/users/guest", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Local-Token": token,
        },
        body: JSON.stringify({ tracking: { source: "playwright" } }),
      });
      return { status: response.status, body: await response.text() };
    });
    if (guestAccess.status !== 200) {
      throw new Error(`Playwright 默认游客登录失败: ${guestAccess.body}`);
    }
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
