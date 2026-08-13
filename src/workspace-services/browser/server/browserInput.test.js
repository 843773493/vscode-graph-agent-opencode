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
});
