import { describe, expect, test } from "bun:test";
import { FIXED_BROWSER_ID, encodeServerMessage, validateClientMessage } from "../src/server/protocol.js";

describe("remote screen protocol", () => {
  test("接受合法 attach 消息", () => {
    const message = validateClientMessage({
      type: "attach",
      browserId: FIXED_BROWSER_ID,
    });

    expect(message.type).toBe("attach");
    expect(message.browserId).toBe(FIXED_BROWSER_ID);
  });

  test("拒绝错误 browserId", () => {
    expect(() => validateClientMessage({
      type: "attach",
      browserId: "uuid:13",
    })).toThrow("browserId 必须是 uuid:12");
  });

  test("补齐 pointer 默认字段", () => {
    const message = validateClientMessage({
      type: "pointer",
      browserId: FIXED_BROWSER_ID,
      action: "move",
      x: 10,
      y: 20,
    });

    expect(message.button).toBe("none");
    expect(message.modifiers).toEqual({
      alt: false,
      ctrl: false,
      meta: false,
      shift: false,
    });
  });

  test("校验 key 消息", () => {
    const message = validateClientMessage({
      type: "key",
      browserId: FIXED_BROWSER_ID,
      action: "down",
      key: "a",
      code: "KeyA",
      text: "a",
      modifiers: { shift: false },
    });

    expect(message.repeat).toBe(false);
    expect(message.text).toBe("a");
  });

  test("拒绝过大的 viewport", () => {
    expect(() => validateClientMessage({
      type: "viewport",
      browserId: FIXED_BROWSER_ID,
      width: 6000,
      height: 4000,
    })).toThrow("viewport 过大");
  });

  test("编码服务端消息", () => {
    expect(encodeServerMessage({
      type: "detached",
      browserId: FIXED_BROWSER_ID,
    })).toBe(JSON.stringify({
      type: "detached",
      browserId: FIXED_BROWSER_ID,
    }));
  });
});
