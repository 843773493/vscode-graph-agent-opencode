import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";

let windowListeners = new Map();
let windowTimers = new Map();
let nextWindowTimerId = 0;

function scheduleWindowTimeout(callback, delay) {
  const timerId = ++nextWindowTimerId;
  windowTimers.set(timerId, { callback, delay });
  return timerId;
}

function clearWindowTimeout(timerId) {
  windowTimers.delete(timerId);
}

function flushWindowTimeouts() {
  const scheduledTimers = [...windowTimers.values()];
  windowTimers.clear();
  for (const { callback } of scheduledTimers) {
    callback();
  }
}

class FakeElement {
  constructor() {
    this.listeners = new Map();
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.className = "";
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, listener);
  }

  dispatch(eventName, event = {}) {
    this.listeners.get(eventName)?.(event);
  }
}

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.listeners = new Map();
    this.sentMessages = [];
    FakeWebSocket.instances.push(this);
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, listener);
  }

  send(message) {
    this.sentMessages.push(message);
  }

  emit(eventName, event = {}) {
    this.listeners.get(eventName)?.(event);
  }
}

class FakeTerminal {
  static instances = [];

  constructor() {
    this.cols = 80;
    this.rows = 24;
    FakeTerminal.instances.push(this);
  }

  loadAddon(addon) {
    addon.activate(this);
  }

  open() {}
  write() {}
  onData() {}
}

class FakeFitAddon {
  static instances = [];

  constructor() {
    this.dimensions = { cols: 80, rows: 24 };
    this.fitCalls = 0;
    this.dimensionReads = 0;
    FakeFitAddon.instances.push(this);
  }

  activate(terminal) {
    this.terminal = terminal;
  }

  fit() {
    this.fitCalls += 1;
    const dimensions = this.proposeDimensions();
    if (dimensions) {
      this.terminal.cols = dimensions.cols;
      this.terminal.rows = dimensions.rows;
    }
  }

  proposeDimensions() {
    this.dimensionReads += 1;
    return { ...this.dimensions };
  }
}

const selectors = [
  "#terminal-host",
  "#attach-toggle",
  "#agent-send",
  "#agent-command",
  "#status-dot",
  "#status-text",
];
let elements = new Map(selectors.map((selector) => [selector, new FakeElement()]));
const savedGlobalDescriptors = new Map();
let clientImportId = 0;

function installGlobal(name, value) {
  savedGlobalDescriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  });
}

function restoreGlobals() {
  for (const [name, descriptor] of savedGlobalDescriptors) {
    if (descriptor) {
      Object.defineProperty(globalThis, name, descriptor);
    } else {
      delete globalThis[name];
    }
  }
}

function resetFixtureState() {
  windowListeners = new Map();
  windowTimers = new Map();
  nextWindowTimerId = 0;
  elements = new Map(selectors.map((selector) => [selector, new FakeElement()]));
  FakeWebSocket.instances.length = 0;
  FakeTerminal.instances.length = 0;
  FakeFitAddon.instances.length = 0;
}

describe("终端浏览器连接状态", () => {
  beforeAll(async () => {
    installGlobal("document", {
      querySelector: (selector) => elements.get(selector),
    });
    installGlobal("window", {
      location: { protocol: "http:", host: "127.0.0.1:8120" },
      addEventListener: (eventName, listener) => windowListeners.set(eventName, listener),
      setTimeout: scheduleWindowTimeout,
      clearTimeout: clearWindowTimeout,
    });
    installGlobal("WebSocket", FakeWebSocket);
    installGlobal("Terminal", FakeTerminal);
    installGlobal("FitAddon", {
      FitAddon: FakeFitAddon,
    });
  });

  beforeEach(async () => {
    resetFixtureState();
    await import(`../src/client/main.js?fixture=${++clientImportId}`);
  });

  afterEach(() => {
    expect(windowTimers.size).toBe(0);
  });

  afterAll(restoreGlobals);

  test("终端尺寸不变时窗口 resize 只读取一次布局且不重复拟合", () => {
    const fitAddon = FakeFitAddon.instances.at(-1);
    const previousFitCalls = fitAddon.fitCalls;
    const previousDimensionReads = fitAddon.dimensionReads;

    windowListeners.get("resize")();
    expect(windowTimers.size).toBe(1);
    expect([...windowTimers.values()][0].delay).toBe(80);
    flushWindowTimeouts();

    expect(fitAddon.dimensionReads - previousDimensionReads).toBe(1);
    expect(fitAddon.fitCalls - previousFitCalls).toBe(0);
  });

  test("重复窗口 resize 事件合并，尺寸改变后只发送一次 PTY resize", () => {
    const attachToggle = elements.get("#attach-toggle");
    attachToggle.dispatch("click");
    const socket = FakeWebSocket.instances.at(-1);
    socket.readyState = FakeWebSocket.OPEN;
    socket.emit("open");
    socket.emit("message", {
      data: JSON.stringify({ type: "attached", terminalId: "uuid:12" }),
    });

    const fitAddon = FakeFitAddon.instances.at(-1);
    const previousFitCalls = fitAddon.fitCalls;
    fitAddon.dimensions = { cols: 100, rows: 30 };
    windowListeners.get("resize")();
    windowListeners.get("resize")();
    expect(windowTimers.size).toBe(1);
    flushWindowTimeouts();

    const resizeMessages = socket.sentMessages
      .map((message) => JSON.parse(message))
      .filter((message) => message.type === "resize");
    expect(resizeMessages).toEqual([{
      type: "resize",
      terminalId: "uuid:12",
      cols: 100,
      rows: 30,
    }]);
    expect(fitAddon.fitCalls - previousFitCalls).toBe(1);

    socket.readyState = FakeWebSocket.CLOSED;
    socket.emit("close", { code: 1000 });
  });

  test("detach 发送失败后，socket 关闭仍更新 UI 状态", () => {
    const attachToggle = elements.get("#attach-toggle");
    const statusText = elements.get("#status-text");
    attachToggle.dispatch("click");
    const socket = FakeWebSocket.instances.at(-1);

    socket.readyState = FakeWebSocket.OPEN;
    socket.emit("open");
    socket.emit("message", {
      data: JSON.stringify({ type: "attached", terminalId: "uuid:12" }),
    });
    expect(statusText.textContent).toBe("已 attach 到 uuid:12");

    socket.readyState = FakeWebSocket.CLOSING;
    expect(() => attachToggle.dispatch("click")).toThrow("WebSocket 未连接");

    socket.readyState = FakeWebSocket.CLOSED;
    socket.emit("close", { code: 1006 });
    expect(attachToggle.textContent).toBe("Attach");
    expect(statusText.textContent).toBe("连接已关闭");
  });

  test("服务端错误详情在 socket 关闭后仍保留", () => {
    const attachToggle = elements.get("#attach-toggle");
    const statusDot = elements.get("#status-dot");
    const statusText = elements.get("#status-text");
    attachToggle.dispatch("click");
    const socket = FakeWebSocket.instances.at(-1);

    socket.readyState = FakeWebSocket.OPEN;
    socket.emit("open");
    socket.emit("message", {
      data: JSON.stringify({ type: "error", message: "PTY resize failed" }),
    });
    socket.readyState = FakeWebSocket.CLOSED;
    socket.emit("close", { code: 1002 });

    expect(attachToggle.textContent).toBe("Attach");
    expect(statusDot.className).toBe("status-dot error");
    expect(statusText.textContent).toBe("PTY resize failed");
  });
});
