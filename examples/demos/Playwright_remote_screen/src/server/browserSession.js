import { EventEmitter } from "node:events";
import { chromium } from "playwright";
import { FIXED_BROWSER_ID } from "./protocol.js";
import { normalizeHttpUrl } from "./url.js";

const DEFAULT_VIEWPORT = Object.freeze({ width: 1280, height: 800 });
const DEFAULT_INITIAL_URL = "https://www.bilibili.com/";
const NAVIGATION_TIMEOUT_MS = 30000;

const SPECIAL_KEY_CODES = new Map([
  ["Backspace", 8],
  ["Tab", 9],
  ["Enter", 13],
  ["Shift", 16],
  ["Control", 17],
  ["Alt", 18],
  ["Pause", 19],
  ["CapsLock", 20],
  ["Escape", 27],
  [" ", 32],
  ["PageUp", 33],
  ["PageDown", 34],
  ["End", 35],
  ["Home", 36],
  ["ArrowLeft", 37],
  ["ArrowUp", 38],
  ["ArrowRight", 39],
  ["ArrowDown", 40],
  ["Insert", 45],
  ["Delete", 46],
  ["Meta", 91],
  ["F1", 112],
  ["F2", 113],
  ["F3", 114],
  ["F4", 115],
  ["F5", 116],
  ["F6", 117],
  ["F7", 118],
  ["F8", 119],
  ["F9", 120],
  ["F10", 121],
  ["F11", 122],
  ["F12", 123],
]);

function modifierBitmask(modifiers = {}) {
  return (modifiers.alt ? 1 : 0)
    | (modifiers.ctrl ? 2 : 0)
    | (modifiers.meta ? 4 : 0)
    | (modifiers.shift ? 8 : 0);
}

function windowsVirtualKeyCodeFor(key, code) {
  if (SPECIAL_KEY_CODES.has(key)) {
    return SPECIAL_KEY_CODES.get(key);
  }
  if (/^Key[A-Z]$/.test(code)) {
    return code.charCodeAt(3);
  }
  if (/^Digit\d$/.test(code)) {
    return code.charCodeAt(5);
  }
  if (key.length === 1) {
    return key.toUpperCase().charCodeAt(0);
  }
  return 0;
}

function browserLaunchArgs(cdpHost, cdpPort) {
  const args = ["--disable-dev-shm-usage"];
  args.push(`--remote-debugging-address=${cdpHost}`);
  args.push(`--remote-debugging-port=${cdpPort}`);
  if (process.platform === "linux") {
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
    this.attachedClientIds = new Set();
  }

  async start() {
    this.browser = await chromium.launch({
      headless: this.headless,
      args: browserLaunchArgs(this.cdpHost, this.cdpPort),
    });
    this.context = await this.browser.newContext({
      viewport: this.viewport,
      deviceScaleFactor: 1,
      ignoreHTTPSErrors: true,
    });
    this.page = await this.context.newPage();
    this.page.on("framenavigated", (frame) => {
      if (frame === this.page.mainFrame()) {
        void this.emitState();
      }
    });
    this.page.on("domcontentloaded", () => {
      void this.emitState();
    });
    this.page.on("load", () => {
      void this.emitState();
    });
    this.page.on("close", () => {
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
  }

  assertStarted() {
    if (!this.browser || !this.context || !this.page || !this.cdpSession) {
      throw new Error("Playwright 浏览器会话尚未启动");
    }
  }

  async close() {
    this.assertStarted();
    if (this.streaming) {
      await this.stopScreencast();
    }
    await this.browser.close();
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

  async attachClient(clientId) {
    this.assertStarted();
    this.attachedClientIds.add(clientId);
    if (!this.streaming) {
      await this.startScreencast();
    }
    return await this.emitState();
  }

  async detachClient(clientId) {
    this.assertStarted();
    this.attachedClientIds.delete(clientId);
    if (this.attachedClientIds.size === 0 && this.streaming) {
      await this.stopScreencast();
    }
    return await this.emitState();
  }

  async startScreencast() {
    this.assertStarted();
    await this.cdpSession.send("Page.startScreencast", {
      format: "jpeg",
      quality: 78,
      maxWidth: this.viewport.width,
      maxHeight: this.viewport.height,
      everyNthFrame: 1,
    });
    this.streaming = true;
  }

  async stopScreencast() {
    this.assertStarted();
    await this.cdpSession.send("Page.stopScreencast");
    this.streaming = false;
  }

  async handleScreencastFrame(event) {
    this.assertStarted();
    await this.cdpSession.send("Page.screencastFrameAck", {
      sessionId: event.sessionId,
    });
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
    if (this.streaming) {
      await this.stopScreencast();
      await this.startScreencast();
    }
    return await this.emitState();
  }

  async dispatchPointer(message) {
    this.assertStarted();
    const typeByAction = {
      move: "mouseMoved",
      down: "mousePressed",
      up: "mouseReleased",
      wheel: "mouseWheel",
    };
    const params = {
      type: typeByAction[message.action],
      x: message.x,
      y: message.y,
      button: message.action === "move" || message.action === "wheel" ? "none" : message.button,
      modifiers: modifierBitmask(message.modifiers),
    };
    if (message.action === "down" || message.action === "up") {
      params.clickCount = 1;
    }
    if (message.action === "wheel") {
      params.deltaX = message.deltaX;
      params.deltaY = message.deltaY;
    }
    await this.cdpSession.send("Input.dispatchMouseEvent", params);
  }

  async dispatchKey(message) {
    this.assertStarted();
    const isKeyDown = message.action === "down";
    const text = isKeyDown && typeof message.text === "string" ? message.text : "";
    const keyCode = windowsVirtualKeyCodeFor(message.key, message.code);
    const params = {
      type: isKeyDown && text ? "keyDown" : isKeyDown ? "rawKeyDown" : "keyUp",
      key: message.key,
      code: message.code,
      windowsVirtualKeyCode: keyCode,
      nativeVirtualKeyCode: keyCode,
      modifiers: modifierBitmask(message.modifiers),
      autoRepeat: message.repeat,
    };
    if (text) {
      params.text = text;
      params.unmodifiedText = text;
    }
    await this.cdpSession.send("Input.dispatchKeyEvent", params);
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
