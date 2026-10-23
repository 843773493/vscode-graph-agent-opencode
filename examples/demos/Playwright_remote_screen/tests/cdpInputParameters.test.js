import { describe, expect, test } from "bun:test";
import {
  toCdpKeyEventParameters,
  toCdpMouseEventParameters,
} from "../src/server/cdpInputParameters.js";

describe("CDP input parameters", () => {
  test("鼠标 move 不携带按钮，wheel 保留 delta", () => {
    expect(toCdpMouseEventParameters({
      action: "move",
      x: 12,
      y: 34,
      button: "left",
      modifiers: {},
    })).toEqual({
      type: "mouseMoved",
      x: 12,
      y: 34,
      button: "none",
      modifiers: 0,
    });
    expect(toCdpMouseEventParameters({
      action: "wheel",
      x: 12,
      y: 34,
      button: "none",
      deltaX: 5,
      deltaY: -9,
      modifiers: { ctrl: true, shift: true },
    })).toEqual({
      type: "mouseWheel",
      x: 12,
      y: 34,
      button: "none",
      modifiers: 10,
      deltaX: 5,
      deltaY: -9,
    });
  });

  test("键盘文本与特殊键生成 CDP 使用的虚拟键码", () => {
    expect(toCdpKeyEventParameters({
      action: "down",
      key: "a",
      code: "KeyA",
      text: "a",
      repeat: false,
      modifiers: { alt: true, meta: true },
    })).toEqual({
      type: "keyDown",
      key: "a",
      code: "KeyA",
      windowsVirtualKeyCode: 65,
      nativeVirtualKeyCode: 65,
      modifiers: 5,
      autoRepeat: false,
      text: "a",
      unmodifiedText: "a",
    });
    expect(toCdpKeyEventParameters({
      action: "down",
      key: "ArrowLeft",
      code: "ArrowLeft",
      text: "",
      repeat: true,
      modifiers: {},
    })).toMatchObject({
      type: "rawKeyDown",
      windowsVirtualKeyCode: 37,
      nativeVirtualKeyCode: 37,
      autoRepeat: true,
    });
  });
});
