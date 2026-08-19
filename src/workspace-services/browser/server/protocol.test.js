import { describe, expect, test } from "bun:test";
import { encodeServerMessage, parseClientMessage } from "./protocol.js";

describe("浏览器元素选择协议", () => {
  test("接受元素悬停与选择坐标", () => {
    expect(parseClientMessage(JSON.stringify({
      type: "inspectElement",
      browserId: "browser_1",
      x: 12.5,
      y: 24,
    })).type).toBe("inspectElement");
    expect(parseClientMessage(JSON.stringify({
      type: "selectElement",
      browserId: "browser_1",
      x: 12.5,
      y: 24,
    })).type).toBe("selectElement");
  });

  test("拒绝非法坐标", () => {
    expect(() => parseClientMessage(JSON.stringify({
      type: "selectElement",
      browserId: "browser_1",
      x: -1,
      y: 24,
    }))).toThrow("x/y 不能为负数");
  });

  test("允许服务端返回元素命中结果", () => {
    expect(JSON.parse(encodeServerMessage({
      type: "elementHovered",
      browserId: "browser_1",
      element: null,
    })).type).toBe("elementHovered");
  });
});

describe("浏览器指针协议", () => {
  test("保留拖拽按键状态和双击次数", () => {
    const message = parseClientMessage(JSON.stringify({
      type: "pointer",
      browserId: "browser_1",
      action: "move",
      button: "none",
      buttons: 1,
      clickCount: 2,
      x: 12,
      y: 24,
    }));
    expect(message.buttons).toBe(1);
    expect(message.clickCount).toBe(2);
  });

  test("拒绝非法指针状态", () => {
    expect(() => parseClientMessage(JSON.stringify({
      type: "pointer",
      browserId: "browser_1",
      action: "down",
      button: "left",
      buttons: 64,
      clickCount: 4,
      x: 12,
      y: 24,
    }))).toThrow("buttons 必须是 0 到 31 的整数");
  });
});

describe("浏览器页面对话框协议", () => {
  test("接受用户设备选择的文件内容", () => {
    const message = parseClientMessage(JSON.stringify({
      type: "selectFiles",
      browserId: "browser_1",
      files: [{
        name: "hello.txt",
        mimeType: "text/plain",
        data: Buffer.from("你好").toString("base64"),
      }],
    }));
    expect(message.files[0].name).toBe("hello.txt");
  });

  test("拒绝带路径的文件名", () => {
    expect(() => parseClientMessage(JSON.stringify({
      type: "selectFiles",
      browserId: "browser_1",
      files: [{ name: "../secret", mimeType: "text/plain", data: "" }],
    }))).toThrow("文件名不能为空且不能包含路径分隔符");
  });
});

describe("浏览器键盘协议", () => {
  test("空格是合法按键", () => {
    const message = parseClientMessage(JSON.stringify({
      type: "key",
      browserId: "browser_1",
      action: "down",
      key: " ",
      code: "Space",
      text: " ",
    }));
    expect(message.key).toBe(" ");
  });
});

describe("浏览器画面确认协议", () => {
  test("接受客户端绘制耗时", () => {
    const message = parseClientMessage(JSON.stringify({
      type: "frameAck",
      browserId: "browser_1",
      frameId: 42,
      decodeMs: 3.5,
      drawMs: 1.25,
    }));
    expect(message.frameId).toBe(42);
  });

  test("拒绝非法画面序号", () => {
    expect(() => parseClientMessage(JSON.stringify({
      type: "frameAck",
      browserId: "browser_1",
      frameId: 0,
    }))).toThrow("frameId 必须是正整数");
  });
});

describe("浏览器设备模拟协议", () => {
  test("接受设备配置与方向", () => {
    const message = parseClientMessage(JSON.stringify({
      type: "deviceProfile",
      browserId: "browser_1",
      profileId: "iphone-13",
      orientation: "landscape",
    }));
    expect(message.profileId).toBe("iphone-13");
    expect(message.orientation).toBe("landscape");
  });

  test("拒绝未知设备方向", () => {
    expect(() => parseClientMessage(JSON.stringify({
      type: "deviceProfile",
      browserId: "browser_1",
      profileId: "iphone-13",
      orientation: "diagonal",
    }))).toThrow("未知浏览器设备方向");
  });

  test("接受响应式设备设置", () => {
    const message = parseClientMessage(JSON.stringify({
      type: "deviceSettings",
      browserId: "browser_1",
      settings: {
        width: 264,
        height: 478,
        deviceScaleFactor: 1,
        touchSimulation: true,
        networkProfileId: "slow-3g",
      },
    }));
    expect(message.settings).toMatchObject({ width: 264, height: 478, networkProfileId: "slow-3g" });
  });

  test("接受调试快照和截图请求", () => {
    expect(parseClientMessage(JSON.stringify({
      type: "debugSnapshot",
      browserId: "browser_1",
      panel: "all",
    })).type).toBe("debugSnapshot");
    expect(parseClientMessage(JSON.stringify({
      type: "captureScreenshot",
      browserId: "browser_1",
    })).type).toBe("captureScreenshot");
    expect(parseClientMessage(JSON.stringify({
      type: "command",
      browserId: "browser_1",
      name: "clearNetwork",
    })).name).toBe("clearNetwork");
  });
});
