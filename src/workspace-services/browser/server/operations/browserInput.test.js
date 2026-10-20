import { describe, expect, test } from "bun:test";
import { EventEmitter } from "node:events";
import { BrowserPointerController, dispatchPointer } from "./browserInput.js";

describe("浏览器 CDP 指针分发", () => {
  test("移动时保留左键按下状态以支持拖拽", async () => {
    const calls = [];
    await dispatchPointer({
      send: async (method, params) => calls.push({ method, params }),
    }, {
      action: "move",
      button: "left",
      buttons: 1,
      x: 20,
      y: 30,
      modifiers: {},
    });

    expect(calls).toEqual([{
      method: "Input.dispatchMouseEvent",
      params: {
        type: "mouseMoved",
        x: 20,
        y: 30,
        button: "left",
        modifiers: 0,
        buttons: 1,
        force: 0.5,
      },
    }]);
  });

  test("HTML5 拖拽被 Chromium 截获后发送 dragEnter/dragOver/drop", async () => {
    const cdp = new EventEmitter();
    const calls = [];
    cdp.send = async (method, params) => {
      calls.push({ method, params });
      if (method === "Input.dispatchMouseEvent" && params.type === "mouseMoved") {
        cdp.emit("Input.dragIntercepted", {
          data: { items: [], dragOperationsMask: 1 },
        });
      }
    };
    const controller = new BrowserPointerController();
    const base = { button: "left", buttons: 1, clickCount: 1, modifiers: {} };

    await controller.dispatch(cdp, { ...base, action: "down", x: 10, y: 10 });
    await controller.dispatch(cdp, { ...base, action: "move", x: 20, y: 20 });
    await controller.dispatch(cdp, { ...base, action: "move", x: 30, y: 30 });
    await controller.dispatch(cdp, { ...base, action: "up", buttons: 0, x: 40, y: 40 });

    expect(calls.filter((call) => call.method === "Input.dispatchDragEvent").map((call) => call.params.type))
      .toEqual(["dragEnter", "dragOver", "drop"]);
  });

  test("双击次数传给 Chromium", async () => {
    const calls = [];
    await dispatchPointer({
      send: async (method, params) => calls.push({ method, params }),
    }, {
      action: "down",
      button: "left",
      buttons: 1,
      clickCount: 2,
      x: 20,
      y: 30,
      modifiers: {},
    });

    expect(calls[0].params.clickCount).toBe(2);
  });

  test("拖拽拦截启用失败保留原始错误，重复尝试不积累监听器", async () => {
    const cdp = new EventEmitter();
    const failure = new Error("模拟 CDP 拒绝启用拖拽拦截");
    cdp.send = async (method) => {
      if (method === "Input.setInterceptDrags") throw failure;
    };
    const controller = new BrowserPointerController();
    const base = { button: "left", buttons: 1, modifiers: {} };
    await controller.dispatch(cdp, { ...base, action: "down", x: 10, y: 10 });

    for (let attempt = 0; attempt < 3; attempt += 1) {
      await expect(controller.dispatch(cdp, { ...base, action: "move", x: 20, y: 20 }))
        .rejects.toBe(failure);
      expect(cdp.listenerCount("Input.dragIntercepted")).toBe(0);
    }
    await controller.reset();
    expect(cdp.listenerCount("Input.dragIntercepted")).toBe(0);
  });

  test("首次启用失败后重试成功，完成拖拽并释放唯一监听器", async () => {
    const cdp = new EventEmitter();
    const calls = [];
    const failure = new Error("模拟首次启用失败");
    let enableAttempts = 0;
    cdp.send = async (method, params) => {
      calls.push({ method, params });
      if (method === "Input.setInterceptDrags" && params.enabled && ++enableAttempts === 1) {
        throw failure;
      }
    };
    const controller = new BrowserPointerController();
    const base = { button: "left", buttons: 1, modifiers: {} };
    await controller.dispatch(cdp, { ...base, action: "down", x: 10, y: 10 });
    await expect(controller.dispatch(cdp, { ...base, action: "move", x: 20, y: 20 }))
      .rejects.toBe(failure);

    await controller.dispatch(cdp, { ...base, action: "move", x: 20, y: 20 });
    expect(cdp.listenerCount("Input.dragIntercepted")).toBe(1);
    const dragData = { items: [], dragOperationsMask: 1 };
    cdp.emit("Input.dragIntercepted", { data: dragData });
    await controller.dispatch(cdp, { ...base, action: "move", x: 30, y: 30 });
    await controller.dispatch(cdp, { ...base, action: "up", buttons: 0, x: 40, y: 40 });

    const dragEvents = calls.filter((call) => call.method === "Input.dispatchDragEvent");
    expect(dragEvents.map((call) => call.params.type)).toEqual(["dragEnter", "dragOver", "drop"]);
    expect(dragEvents.every((call) => call.params.data === dragData)).toBe(true);
    expect(cdp.listenerCount("Input.dragIntercepted")).toBe(0);
    expect(calls.at(-1)).toEqual({ method: "Input.setInterceptDrags", params: { enabled: false } });
  });

  test("启用拦截的确认返回前到达的拖拽事件仍能被消费", async () => {
    const cdp = new EventEmitter();
    const calls = [];
    const dragData = { items: [], dragOperationsMask: 1 };
    cdp.send = async (method, params) => {
      calls.push({ method, params });
      if (method === "Input.setInterceptDrags" && params.enabled) {
        cdp.emit("Input.dragIntercepted", { data: dragData });
      }
    };
    const controller = new BrowserPointerController();
    const base = { button: "left", buttons: 1, modifiers: {} };
    await controller.dispatch(cdp, { ...base, action: "down", x: 10, y: 10 });
    await controller.dispatch(cdp, { ...base, action: "move", x: 20, y: 20 });
    await controller.dispatch(cdp, { ...base, action: "up", buttons: 0, x: 30, y: 30 });

    const dragEvents = calls.filter((call) => call.method === "Input.dispatchDragEvent");
    expect(dragEvents.map((call) => call.params.type)).toEqual(["dragEnter", "dragOver", "drop"]);
    expect(dragEvents.every((call) => call.params.data === dragData)).toBe(true);
    expect(cdp.listenerCount("Input.dragIntercepted")).toBe(0);
  });
});
