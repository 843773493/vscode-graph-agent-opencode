import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";

const selectors = [
  "#browser-id",
  "#browser-id-input",
  "#attach-toggle",
  "#back-button",
  "#forward-button",
  "#reload-button",
  "#stop-button",
  "#devtools-button",
  "#url-form",
  "#address-input",
  "#go-button",
  "#screen-stage",
  "#screen-scroll",
  "#screen-canvas",
  "#screen-overlay",
  "#status-dot",
  "#status-text",
  "#page-title",
];
let listenersByEvent = new Map();
let timers = new Map();
let nextTimerId = 0;
const canvasContext = {
  fillRect() {},
  drawImage() {},
};

function scheduleTimeout(callback, delay) {
  const timerId = ++nextTimerId;
  timers.set(timerId, { callback, delay });
  return timerId;
}

function clearTimeoutById(timerId) {
  timers.delete(timerId);
}

function flushTimeouts() {
  const pendingTimers = [...timers.values()];
  timers.clear();
  for (const { callback } of pendingTimers) {
    callback();
  }
}

class FakeElement {
  constructor(selector) {
    this.selector = selector;
    this.listeners = new Map();
    this.textContent = "";
    this.value = selector === "#browser-id-input"
      ? "uuid:12"
      : selector === "#address-input"
        ? "https://example.test/"
        : "";
    this.disabled = false;
    this.hidden = false;
    this.width = 1280;
    this.height = 800;
    this.className = "";
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, listener);
  }

  dispatch(eventName, event = {}) {
    this.listeners.get(eventName)?.({ preventDefault() {}, ...event });
  }

  getContext() {
    return canvasContext;
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, width: 900, height: 650 };
  }

  focus() {
    globalThis.document.activeElement = this;
  }

  setPointerCapture() {}
  hasPointerCapture() { return false; }
  releasePointerCapture() {}
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
    this.sentMessages.push(JSON.parse(message));
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
  }

  emit(eventName, event = {}) {
    this.listeners.get(eventName)?.(event);
  }
}

class FakeImage {
  set src(value) {
    this.source = value;
  }

  removeAttribute(attributeName) {
    if (attributeName === "src") {
      this.source = "";
    }
  }
}

class FakeResizeObserver {
  static instances = [];

  constructor(callback) {
    this.callback = callback;
    FakeResizeObserver.instances.push(this);
  }

  observe() {}
}

let elements = new Map(selectors.map((selector) => [selector, new FakeElement(selector)]));
const savedGlobalDescriptors = new Map();
let browserEntryImportId = 0;

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
  listenersByEvent = new Map();
  timers = new Map();
  nextTimerId = 0;
  elements = new Map(selectors.map((selector) => [selector, new FakeElement(selector)]));
  FakeWebSocket.instances.length = 0;
  FakeResizeObserver.instances.length = 0;
  document.activeElement = null;
}

describe("远程屏幕浏览器入口", () => {
  beforeAll(async () => {
    installGlobal("document", {
      querySelector: (selector) => elements.get(selector),
    });
    installGlobal("window", {
      location: {
        origin: "http://127.0.0.1:8140",
        protocol: "http:",
        host: "127.0.0.1:8140",
      },
      addEventListener: (eventName, listener) => listenersByEvent.set(eventName, listener),
      requestAnimationFrame: (callback) => scheduleTimeout(callback, 16),
      cancelAnimationFrame: clearTimeoutById,
      setTimeout: scheduleTimeout,
      clearTimeout: clearTimeoutById,
      open() {},
    });
    installGlobal("WebSocket", FakeWebSocket);
    installGlobal("Image", FakeImage);
    installGlobal("ResizeObserver", FakeResizeObserver);
  });

  beforeEach(async () => {
    resetFixtureState();
    await import(`../src/client/main.js?fixture=${++browserEntryImportId}`);
  });

  afterEach(() => {
    expect(timers.size).toBe(0);
  });

  afterAll(restoreGlobals);

  test("detach 会先发送最新指针、取消 viewport 并丢弃 detach 后输入", () => {
    const attachToggle = elements.get("#attach-toggle");
    attachToggle.dispatch("click");
    const socket = FakeWebSocket.instances.at(-1);
    socket.readyState = FakeWebSocket.OPEN;
    socket.emit("open");
    socket.emit("message", {
      data: JSON.stringify({
        type: "attached",
        browserId: "uuid:12",
        state: {
          browserId: "uuid:12",
          closed: false,
          url: "https://example.test/",
          title: "Example",
          viewport: { width: 1920, height: 650 },
          attachedClients: 1,
          streaming: false,
        },
      }),
    });

    expect(timers.size).toBe(1);
    expect([...timers.values()][0].delay).toBe(80);

    const canvas = elements.get("#screen-canvas");
    canvas.dispatch("pointermove", { clientX: 120, clientY: 140 });
    expect(timers.size).toBe(2);

    attachToggle.dispatch("click");
    FakeResizeObserver.instances.at(-1).callback();
    expect([...timers.values()].some(({ delay }) => delay === 80)).toBe(false);
    expect(socket.sentMessages.map((message) => message.type)).toEqual(["attach", "pointer", "detach"]);

    flushTimeouts();
    canvas.dispatch("pointermove", { clientX: 200, clientY: 220 });
    expect(timers.size).toBe(0);
    flushTimeouts();

    expect(socket.sentMessages.map((message) => message.type)).toEqual(["attach", "pointer", "detach"]);

    socket.emit("message", {
      data: JSON.stringify({
        type: "detached",
        browserId: "uuid:12",
        state: {
          browserId: "uuid:12",
          closed: false,
          url: "https://example.test/",
          title: "Example",
          viewport: { width: 1920, height: 650 },
          attachedClients: 0,
          streaming: false,
        },
      }),
    });
    socket.emit("close");
  });

  test("detach 失败时关闭连接并由 close 清理本地附着状态", () => {
    const attachToggle = elements.get("#attach-toggle");
    attachToggle.dispatch("click");
    const socket = FakeWebSocket.instances.at(-1);
    socket.readyState = FakeWebSocket.OPEN;
    socket.emit("open");
    socket.emit("message", {
      data: JSON.stringify({
        type: "attached",
        browserId: "uuid:12",
        state: {
          browserId: "uuid:12",
          closed: false,
          url: "https://example.test/",
          title: "Example",
          viewport: { width: 1920, height: 650 },
          attachedClients: 1,
          streaming: false,
        },
      }),
    });

    attachToggle.dispatch("click");
    socket.emit("message", {
      data: JSON.stringify({ type: "error", message: "CDP detach failed" }),
    });

    expect(socket.readyState).toBe(FakeWebSocket.CLOSED);
    socket.emit("close");
    expect(attachToggle.textContent).toBe("Attach");
    expect(elements.get("#status-text").textContent).toBe("CDP detach failed");
    expect(timers.size).toBe(0);
  });
});
