import { describe, expect, test } from "bun:test";
import { browserLaunchArgs } from "../src/server/browserSession.js";
import { createBrowserSessionStub } from "./browserSessionStub.js";

function createDeferred() {
  let resolve;
  const promise = new Promise((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

class FakeCdpSession {
  constructor(startGate = null) {
    this.methods = [];
    this.calls = [];
    this.startGate = startGate;
  }

  async send(method, params) {
    this.methods.push(method);
    this.calls.push({ method, params });
    if (method === "Page.startScreencast" && this.startGate) {
      await this.startGate.promise;
    }
  }
}

describe("BrowserSession screencast 生命周期", () => {
  test("Chromium 启动参数只在 Linux 添加容器专用选项", () => {
    const remoteDebuggingArgs = [
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=9333",
    ];

    expect(browserLaunchArgs("127.0.0.1", 9333, "linux")).toEqual([
      "--disable-dev-shm-usage",
      ...remoteDebuggingArgs,
      "--no-sandbox",
    ]);
    expect(browserLaunchArgs("127.0.0.1", 9333, "win32")).toEqual(remoteDebuggingArgs);
    expect(browserLaunchArgs("127.0.0.1", 9333, "darwin")).toEqual(remoteDebuggingArgs);
  });

  test("并发 attach 只启动一个 screencast", async () => {
    const startGate = createDeferred();
    const cdpSession = new FakeCdpSession(startGate);
    const session = createBrowserSessionStub(cdpSession);

    const firstAttach = session.attachClient("viewer:1");
    const secondAttach = session.attachClient("viewer:2");
    await Promise.resolve();

    expect(cdpSession.methods).toEqual(["Page.startScreencast"]);
    startGate.resolve();
    await Promise.all([firstAttach, secondAttach]);

    expect(session.streaming).toBe(true);
    expect(cdpSession.methods).toEqual(["Page.startScreencast"]);
  });

  test("最后一个客户端在启动期间 detach 后会停止 screencast", async () => {
    const startGate = createDeferred();
    const cdpSession = new FakeCdpSession(startGate);
    const session = createBrowserSessionStub(cdpSession);

    const attach = session.attachClient("viewer:1");
    await Promise.resolve();
    const detach = session.detachClient("viewer:1");
    startGate.resolve();
    await Promise.all([attach, detach]);

    expect(session.streaming).toBe(false);
    expect(cdpSession.methods).toEqual([
      "Page.startScreencast",
      "Page.stopScreencast",
    ]);
  });

  test("pointer action 映射到对应的 CDP 鼠标事件", async () => {
    const cdpSession = new FakeCdpSession();
    const session = createBrowserSessionStub(cdpSession);
    const actions = ["move", "down", "up", "wheel"];

    for (const action of actions) {
      await session.dispatchPointer({
        action,
        x: 12,
        y: 34,
        button: "left",
        deltaX: 1,
        deltaY: 2,
        modifiers: {},
      });
    }

    expect(cdpSession.calls.map(({ params }) => params.type)).toEqual([
      "mouseMoved",
      "mousePressed",
      "mouseReleased",
      "mouseWheel",
    ]);
  });

  test("dispatchKey 将浏览器键消息映射到 CDP 键盘事件", async () => {
    const cdpSession = new FakeCdpSession();
    const session = createBrowserSessionStub(cdpSession);

    await session.dispatchKey({
      action: "down",
      key: "a",
      code: "KeyA",
      text: "a",
      repeat: false,
      modifiers: {},
    });

    expect(cdpSession.calls).toEqual([{
      method: "Input.dispatchKeyEvent",
      params: {
        type: "keyDown",
        key: "a",
        code: "KeyA",
        windowsVirtualKeyCode: 65,
        nativeVirtualKeyCode: 65,
        modifiers: 0,
        autoRepeat: false,
        text: "a",
        unmodifiedText: "a",
      },
    }]);
  });
});
