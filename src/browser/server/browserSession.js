import { EventEmitter } from "node:events";
import { chromium } from "playwright";
import { normalizeBrowserUrl, nowIso } from "./url.js";
import { dispatchKey, dispatchPointer, insertText } from "./browserInput.js";
import {
  clickElement,
  dragElement,
  handleDialog,
  hoverElement,
  readBrowserSummary,
  runPlaywrightCode,
  screenshotPage,
  typeInPage,
} from "./browserPageActions.js";
import {
  DEFAULT_VIEWPORT,
  NAVIGATION_TIMEOUT_MS,
  TOOL_TIMEOUT_MS,
  browserLaunchArgs,
} from "./browserRuntime.js";

const BROWSER_MODAL_DETECTION_MS = 1000;

export class BrowserSession extends EventEmitter {
  constructor({ manager, record }) {
    super();
    this.manager = manager;
    this.record = {
      viewport: { ...DEFAULT_VIEWPORT },
      client_count: 0,
      sequence: 0,
      ...record,
    };
    this.browser = null;
    this.context = null;
    this.page = null;
    this.cdpSession = null;
    this.streaming = false;
    this.clients = new Set();
    this.pendingDialog = null;
    this.pendingFileChooser = null;
    this.refSelectors = new Map();
  }

  get id() {
    return this.record.browser_id;
  }

  get sessionId() {
    return this.record.session_id;
  }

  get status() {
    return this.record.status;
  }

  async start() {
    if (this.browser) {
      return this.snapshot();
    }
    this.browser = await chromium.launch({
      headless: true,
      args: browserLaunchArgs(),
    });
    this.context = await this.browser.newContext({
      viewport: this.record.viewport || DEFAULT_VIEWPORT,
      deviceScaleFactor: 1,
      ignoreHTTPSErrors: true,
    });
    this.page = await this.context.newPage();
    this.page.on("framenavigated", (frame) => {
      if (frame === this.page.mainFrame()) {
        void this.syncAndEmitState();
      }
    });
    this.page.on("domcontentloaded", () => {
      void this.syncAndEmitState();
    });
    this.page.on("load", () => {
      void this.syncAndEmitState();
    });
    this.page.on("dialog", (dialog) => {
      this.pendingDialog = {
        type: dialog.type(),
        message: dialog.message(),
        defaultValue: dialog.defaultValue(),
        dialog,
      };
      this.emit("browser-modal", { kind: "dialog" });
      void this.syncAndEmitState();
    });
    this.page.on("filechooser", (fileChooser) => {
      this.pendingFileChooser = fileChooser;
      this.emit("browser-modal", { kind: "filechooser" });
      void this.syncAndEmitState();
    });
    this.page.on("close", () => {
      if (this.record.status !== "running" && this.record.status !== "created") {
        return;
      }
      this.record.status = "closed";
      this.record.ended_at = this.record.ended_at || nowIso();
      this.record.updated_at = nowIso();
      void this.manager.persist();
      this.emit("state", this.snapshot());
    });
    this.cdpSession = await this.context.newCDPSession(this.page);
    await this.cdpSession.send("Page.enable");
    await this.cdpSession.send("Runtime.enable");
    this.cdpSession.on("Page.screencastFrame", (event) => {
      void this.handleScreencastFrame(event);
    });
    this.record.status = "running";
    this.record.started_at = this.record.started_at || nowIso();
    try {
      await this.goto(this.record.url || "about:blank");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.failStartup(message);
      throw error;
    }
    return this.snapshot();
  }

  assertRunning() {
    if (!this.browser || !this.context || !this.page || !this.cdpSession) {
      throw new Error(`浏览器页面尚未启动: ${this.id}`);
    }
    if (this.record.status !== "running") {
      throw new Error(`浏览器页面当前不可操作: browser_id=${this.id}, status=${this.record.status}`);
    }
  }

  snapshot() {
    return {
      ...this.record,
      attach_url: this.manager.attachUrl(this.id),
      client_count: this.clients.size,
      pending_dialog: this.pendingDialog
        ? {
            type: this.pendingDialog.type,
            message: this.pendingDialog.message,
            defaultValue: this.pendingDialog.defaultValue,
          }
        : null,
      pending_file_chooser: this.pendingFileChooser ? true : false,
    };
  }

  async syncPageState() {
    this.assertRunning();
    this.record.url = this.page.url();
    this.record.title = await this.page.title();
    this.record.client_count = this.clients.size;
    this.record.updated_at = nowIso();
    this.record.sequence = Number(this.record.sequence || 0) + 1;
    await this.manager.persist();
    return this.snapshot();
  }

  async syncAndEmitState() {
    const state = await this.syncPageState();
    this.emit("state", state);
    return state;
  }

  async goto(rawUrl) {
    this.assertRunning();
    const url = normalizeBrowserUrl(rawUrl);
    this.record.url = url;
    if (url === "about:blank") {
      await this.page.goto(url);
    } else {
      await this.page.goto(url, {
        waitUntil: "domcontentloaded",
        timeout: NAVIGATION_TIMEOUT_MS,
      });
    }
    return await this.syncAndEmitState();
  }

  async navigate(type = "url", url = null) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(async () => {
      if (type === "reload") {
        await this.page.reload({ waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
      } else if (type === "back") {
        await this.page.goBack({ waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
      } else if (type === "forward") {
        await this.page.goForward({ waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
      } else if (type === "url") {
        if (!url) {
          throw new Error("navigate type=url 需要 url");
        }
        await this.goto(url);
      } else {
        throw new Error(`未知 navigate type: ${type}`);
      }
    });
    return await this.syncAndEmitState();
  }

  async stopLoading() {
    this.assertRunning();
    await this.cdpSession.send("Page.stopLoading");
    return await this.syncAndEmitState();
  }

  async close({ status = "closed", reason = "browser_closed" } = {}) {
    if (this.record.status === "deleted") {
      return this.snapshot();
    }
    if (this.streaming && this.browser && this.cdpSession && this.record.status === "running") {
      await this.stopScreencast();
    }
    this.record.status = status;
    this.record.release_reason = reason;
    this.record.ended_at = this.record.ended_at || nowIso();
    this.record.updated_at = nowIso();
    if (this.browser) {
      await this.browser.close();
    }
    this.browser = null;
    this.context = null;
    this.page = null;
    this.cdpSession = null;
    this.clients.clear();
    await this.manager.persist();
    return this.snapshot();
  }

  async failStartup(message) {
    this.record.status = "failed";
    this.record.release_reason = "browser_initial_navigation_failed";
    this.record.error_message = message;
    this.record.ended_at = this.record.ended_at || nowIso();
    this.record.updated_at = nowIso();
    if (this.browser) {
      await this.browser.close();
    }
    this.browser = null;
    this.context = null;
    this.page = null;
    this.cdpSession = null;
    this.clients.clear();
    await this.manager.persist();
    this.emit("state", this.snapshot());
    return this.snapshot();
  }

  async attachClient(client) {
    this.assertRunning();
    this.clients.add(client);
    if (!this.streaming) {
      await this.startScreencast();
    }
    return await this.syncAndEmitState();
  }

  async detachClient(client) {
    this.clients.delete(client);
    if (this.record.status !== "running" || !this.browser || !this.cdpSession) {
      this.streaming = false;
      this.record.client_count = this.clients.size;
      this.record.updated_at = nowIso();
      await this.manager.persist();
      return this.snapshot();
    }
    if (this.clients.size === 0 && this.streaming) {
      await this.stopScreencast();
    }
    return await this.syncAndEmitState();
  }

  async startScreencast() {
    this.assertRunning();
    await this.cdpSession.send("Page.startScreencast", {
      format: "jpeg",
      quality: 78,
      maxWidth: this.record.viewport?.width || DEFAULT_VIEWPORT.width,
      maxHeight: this.record.viewport?.height || DEFAULT_VIEWPORT.height,
      everyNthFrame: 1,
    });
    this.streaming = true;
  }

  async stopScreencast() {
    this.assertRunning();
    await this.cdpSession.send("Page.stopScreencast");
    this.streaming = false;
  }

  async handleScreencastFrame(event) {
    this.assertRunning();
    await this.cdpSession.send("Page.screencastFrameAck", {
      sessionId: event.sessionId,
    });
    this.emit("frame", {
      browserId: this.id,
      dataUrl: `data:image/jpeg;base64,${event.data}`,
      width: event.metadata.deviceWidth || this.record.viewport?.width || DEFAULT_VIEWPORT.width,
      height: event.metadata.deviceHeight || this.record.viewport?.height || DEFAULT_VIEWPORT.height,
      pageScaleFactor: event.metadata.pageScaleFactor || 1,
      timestamp: event.metadata.timestamp || Date.now() / 1000,
    });
  }

  async setViewport(width, height) {
    this.assertRunning();
    if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0) {
      throw new Error(`非法 viewport: ${width}x${height}`);
    }
    this.record.viewport = { width, height };
    await this.page.setViewportSize(this.record.viewport);
    if (this.streaming) {
      await this.stopScreencast();
      await this.startScreencast();
    }
    return await this.syncAndEmitState();
  }

  async dispatchPointer(message) {
    this.assertRunning();
    await dispatchPointer(this.cdpSession, message);
  }

  async dispatchKey(message) {
    this.assertRunning();
    await dispatchKey(this.cdpSession, message);
  }

  async insertText(text) {
    this.assertRunning();
    await insertText(this.cdpSession, text);
  }

  async readSummary() {
    this.assertRunning();
    const result = await readBrowserSummary(this.page, this.refSelectors);
    await this.syncPageState();
    return result;
  }

  async waitForPendingBrowserModal(timeoutMs = BROWSER_MODAL_DETECTION_MS) {
    if (this.pendingDialog) {
      return "dialog";
    }
    if (this.pendingFileChooser) {
      return "filechooser";
    }
    return await new Promise((resolve) => {
      let settled = false;
      const cleanup = () => {
        clearTimeout(timer);
        this.off("browser-modal", onModal);
      };
      const finish = (kind) => {
        if (settled) {
          return;
        }
        settled = true;
        cleanup();
        resolve(kind);
      };
      const onModal = (event) => {
        finish(event.kind);
      };
      const timer = setTimeout(() => finish(null), timeoutMs);
      this.on("browser-modal", onModal);
    });
  }

  async runPageActionWithModalDetection(action) {
    if (this.pendingDialog || this.pendingFileChooser) {
      throw new Error("当前页面已有待处理浏览器对话框，请先调用 handleDialog。");
    }

    const actionPromise = Promise.resolve().then(action);
    const actionWithFailure = actionPromise.then(
      () => null,
      (error) => {
        throw error;
      },
    );
    const modalKind = await Promise.race([
      actionWithFailure,
      this.waitForPendingBrowserModal(),
    ]);

    if (modalKind) {
      actionPromise.catch(() => undefined);
      await this.syncAndEmitState();
      return modalKind;
    }

    await actionPromise;
    return null;
  }

  async click(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => clickElement(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async hover(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => hoverElement(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async typeInPage(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => typeInPage(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async drag(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => dragElement(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async handleDialog(args) {
    this.assertRunning();
    const result = await handleDialog(this, args);
    return { ...result, state: await this.syncAndEmitState() };
  }

  async screenshot(args) {
    this.assertRunning();
    const buffer = await screenshotPage(this.page, this.refSelectors, args);
    const imagePath = await this.manager.writeScreenshot(this.id, buffer);
    await this.syncAndEmitState();
    return {
      image_path: imagePath,
      mime_type: "image/png",
      byte_length: buffer.byteLength,
    };
  }

  async runPlaywrightCode(args) {
    this.assertRunning();
    let result = null;
    const modalKind = await this.runPageActionWithModalDetection(async () => {
      result = await runPlaywrightCode(
        { page: this.page, context: this.context, browser: this.browser },
        args,
      );
    });
    await this.syncAndEmitState();
    if (modalKind) {
      return {
        result: null,
        summary: `${modalKind === "dialog" ? "Playwright 代码触发了浏览器对话框" : "Playwright 代码触发了文件选择对话框"}，请调用 handleDialog 继续。`,
        state: this.snapshot(),
      };
    }
    return result;
  }
}
