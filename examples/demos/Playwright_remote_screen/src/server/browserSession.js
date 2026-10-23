import { EventEmitter } from "node:events";
import { chromium } from "playwright";
import { FIXED_BROWSER_ID } from "./protocol.js";
import { toCdpKeyEventParameters, toCdpMouseEventParameters } from "./cdpInputParameters.js";
import { normalizeHttpUrl } from "./url.js";

const DEFAULT_VIEWPORT = Object.freeze({ width: 1280, height: 800 });
const DEFAULT_INITIAL_URL = "https://www.bilibili.com/";
const NAVIGATION_TIMEOUT_MS = 30000;
const PAGE_STATE_DEBOUNCE_MS = 50;
export function browserLaunchArgs(cdpHost, cdpPort, platform = process.platform) {
  const args = [
    `--remote-debugging-address=${cdpHost}`,
    `--remote-debugging-port=${cdpPort}`,
  ];
  if (platform === "linux") {
    args.unshift("--disable-dev-shm-usage");
    args.push("--no-sandbox");
  }
  return args;
}

export class BrowserSession extends EventEmitter {
  constructor({
    browserId = FIXED_BROWSER_ID,
    initialUrl = DEFAULT_INITIAL_URL,
    headless = true,
    viewport = DEFAULT_VIEWPORT,
    cdpHost = "127.0.0.1",
    cdpPort = 9333,
  } = {}) {
    super();
    this.browserId = browserId;
    this.initialUrl = normalizeHttpUrl(initialUrl);
    this.headless = headless;
    this.viewport = { ...viewport };
    this.cdpHost = cdpHost;
    this.cdpPort = cdpPort;
    this.browser = null;
    this.context = null;
    this.page = null;
    this.cdpSession = null;
    this.streaming = false;
    this.screencastOperation = Promise.resolve();
    this.attachedClientIds = new Set();
    this.pageStateTimer = null;
  }

  async start() {
    this.browser = await chromium.launch({
      headless: this.headless,
      args: browserLaunchArgs(this.cdpHost, this.cdpPort),
    });
    try {
      this.context = await this.browser.newContext({
        viewport: this.viewport,
        deviceScaleFactor: 1,
        ignoreHTTPSErrors: true,
      });
      this.page = await this.context.newPage();
      this.page.on("framenavigated", (frame) => {
        if (frame === this.page.mainFrame()) {
          this.#emitPageStateIfAttached();
        }
      });
      this.page.on("domcontentloaded", () => {
        this.#emitPageStateIfAttached();
      });
      this.page.on("load", () => {
        this.#emitPageStateIfAttached();
      });
      this.page.on("close", () => {
        this.#cancelPageStateEmission();
        this.emit("state", {
          browserId: this.browserId,
          closed: true,
          url: "",
          title: "",
          viewport: this.viewport,
          attachedClients: this.attachedClientIds.size,
          streaming: false,
        });
      });

      this.cdpSession = await this.context.newCDPSession(this.page);
      await this.cdpSession.send("Page.enable");
      await this.cdpSession.send("Runtime.enable");
      this.cdpSession.on("Page.screencastFrame", (event) => {
        void this.handleScreencastFrame(event);
      });

      return await this.goto(this.initialUrl);
    } catch (error) {
      try {
        await this.close();
      } catch (closeError) {
        throw new AggregateError(
          [error, closeError],
          "浏览器会话启动失败，且无法关闭已启动的 Chromium",
        );
      }
      throw error;
    }
  }

  assertStarted() {
    if (!this.browser || !this.context || !this.page || !this.cdpSession) {
      throw new Error("Playwright 浏览器会话尚未启动");
    }
  }

  async close() {
    this.#cancelPageStateEmission();
    const browser = this.browser;
    if (!browser) {
      return;
    }

    const shutdownErrors = [];
    try {
      await this.#enqueueScreencastOperation(() => this.#setScreencastEnabled(false));
    } catch (error) {
      shutdownErrors.push(error);
    }

    let browserClosed = false;
    try {
      await browser.close();
      browserClosed = true;
    } catch (error) {
      shutdownErrors.push(error);
    }

    if (browserClosed) {
      this.browser = null;
      this.context = null;
      this.page = null;
      this.cdpSession = null;
      this.streaming = false;
    }

    if (shutdownErrors.length === 1) {
      throw shutdownErrors[0];
    }
    if (shutdownErrors.length > 1) {
      throw new AggregateError(shutdownErrors, "浏览器会话关闭失败");
    }
  }

  async currentState() {
    this.assertStarted();
    const title = await this.page.title();
    return {
      browserId: this.browserId,
      closed: this.page.isClosed(),
      url: this.page.url(),
      title,
      viewport: { ...this.viewport },
      attachedClients: this.attachedClientIds.size,
      streaming: this.streaming,
    };
  }

  cdpHttpOrigin() {
    return `http://${this.cdpHost}:${this.cdpPort}`;
  }

  cdpWebSocketOrigin() {
    return `ws://${this.cdpHost}:${this.cdpPort}`;
  }

  async cdpJson(pathname) {
    const response = await fetch(new URL(pathname, this.cdpHttpOrigin()));
    if (!response.ok) {
      throw new Error(`CDP 请求失败 ${pathname}: HTTP ${response.status}`);
    }
    return await response.json();
  }

  async currentDebugTarget() {
    this.assertStarted();
    const targets = await this.cdpJson("/json/list");
    if (!Array.isArray(targets)) {
      throw new Error("CDP /json/list 返回值不是数组");
    }
    const pageUrl = this.page.url();
    const pageTarget = targets.find((target) => target.type === "page" && target.url === pageUrl)
      || targets.find((target) => target.type === "page");
    if (!pageTarget || typeof pageTarget.id !== "string") {
      throw new Error("没有找到可打开 DevTools 的 page target");
    }
    return pageTarget;
  }

  async emitState() {
    const state = await this.currentState();
    this.emit("state", state);
    return state;
  }

  #emitPageStateIfAttached() {
    if (this.attachedClientIds.size === 0) {
      return;
    }
    this.#cancelPageStateEmission();
    this.pageStateTimer = setTimeout(() => {
      this.pageStateTimer = null;
      if (this.attachedClientIds.size > 0) {
        void this.emitState();
      }
    }, PAGE_STATE_DEBOUNCE_MS);
  }

  #cancelPageStateEmission() {
    if (this.pageStateTimer !== null) {
      clearTimeout(this.pageStateTimer);
      this.pageStateTimer = null;
    }
  }

  async attachClient(clientId) {
    this.assertStarted();
    this.attachedClientIds.add(clientId);
    await this.#reconcileScreencast();
    return await this.emitState();
  }

  async detachClient(clientId) {
    this.assertStarted();
    this.attachedClientIds.delete(clientId);
    if (this.attachedClientIds.size === 0) {
      this.#cancelPageStateEmission();
    }
    await this.#reconcileScreencast();
    return await this.emitState();
  }

  #enqueueScreencastOperation(operation) {
    const pendingOperation = this.screencastOperation.then(operation);
    this.screencastOperation = pendingOperation.catch(() => undefined);
    return pendingOperation;
  }

  #reconcileScreencast() {
    return this.#enqueueScreencastOperation(() =>
      this.#setScreencastEnabled(this.attachedClientIds.size > 0));
  }

  async #setScreencastEnabled(enabled) {
    if (enabled === this.streaming) {
      return;
    }
    if (enabled) {
      await this.cdpSession.send("Page.startScreencast", {
        format: "jpeg",
        quality: 78,
        maxWidth: this.viewport.width,
        maxHeight: this.viewport.height,
        everyNthFrame: 1,
      });
      this.streaming = true;
      return;
    }
    await this.cdpSession.send("Page.stopScreencast");
    this.streaming = false;
  }

  #restartScreencast() {
    return this.#enqueueScreencastOperation(async () => {
      if (!this.streaming) {
        return;
      }
      await this.#setScreencastEnabled(false);
      await this.#setScreencastEnabled(this.attachedClientIds.size > 0);
    });
  }

  async handleScreencastFrame(event) {
    this.assertStarted();
    await this.cdpSession.send("Page.screencastFrameAck", {
      sessionId: event.sessionId,
    });
    if (this.attachedClientIds.size === 0) {
      return;
    }
    this.emit("frame", {
      browserId: this.browserId,
      dataUrl: `data:image/jpeg;base64,${event.data}`,
      width: event.metadata.deviceWidth || this.viewport.width,
      height: event.metadata.deviceHeight || this.viewport.height,
      pageScaleFactor: event.metadata.pageScaleFactor || 1,
      timestamp: event.metadata.timestamp || Date.now() / 1000,
    });
  }

  async goto(rawUrl) {
    this.assertStarted();
    const url = normalizeHttpUrl(rawUrl);
    await this.page.goto(url, {
      waitUntil: "domcontentloaded",
      timeout: NAVIGATION_TIMEOUT_MS,
    });
    return await this.emitState();
  }

  async reload() {
    this.assertStarted();
    await this.page.reload({
      waitUntil: "domcontentloaded",
      timeout: NAVIGATION_TIMEOUT_MS,
    });
    return await this.emitState();
  }

  async back() {
    this.assertStarted();
    await this.page.goBack({
      waitUntil: "domcontentloaded",
      timeout: NAVIGATION_TIMEOUT_MS,
    });
    return await this.emitState();
  }

  async forward() {
    this.assertStarted();
    await this.page.goForward({
      waitUntil: "domcontentloaded",
      timeout: NAVIGATION_TIMEOUT_MS,
    });
    return await this.emitState();
  }

  async stopLoading() {
    this.assertStarted();
    await this.cdpSession.send("Page.stopLoading");
    return await this.emitState();
  }

  async setViewport(width, height) {
    this.assertStarted();
    if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0) {
      throw new Error(`非法 viewport: ${width}x${height}`);
    }
    this.viewport = { width, height };
    await this.page.setViewportSize(this.viewport);
    await this.#restartScreencast();
    return await this.emitState();
  }

  async dispatchPointer(message) {
    this.assertStarted();
    await this.cdpSession.send("Input.dispatchMouseEvent", toCdpMouseEventParameters(message));
  }

  async dispatchKey(message) {
    this.assertStarted();
    await this.cdpSession.send("Input.dispatchKeyEvent", toCdpKeyEventParameters(message));
  }

  async insertText(text) {
    this.assertStarted();
    await this.cdpSession.send("Input.insertText", { text });
  }

  async click(x, y) {
    this.assertStarted();
    await this.dispatchPointer({
      action: "down",
      x,
      y,
      button: "left",
      modifiers: {},
    });
    await this.dispatchPointer({
      action: "up",
      x,
      y,
      button: "left",
      modifiers: {},
    });
  }

  async typeText(text) {
    this.assertStarted();
    await this.page.keyboard.insertText(text);
  }

  async pressKey(key) {
    this.assertStarted();
    await this.page.keyboard.press(key);
  }
}
