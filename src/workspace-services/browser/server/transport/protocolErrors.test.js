import { inspect } from "node:util";
import { describe, expect, spyOn, test } from "bun:test";
import { parseClientMessage } from "./protocol.js";

const unknownFields = [
  ["type", value => ({ type: value }), "未知消息类型"],
  ["pointer-action", value => ({ type: "pointer", action: value }), "未知 pointer action"],
  ["pointer-button", value => ({ type: "pointer", action: "down", x: 1, y: 2, button: value }), "未知 pointer button"],
  ["key-action", value => ({ type: "key", action: value }), "未知 key action"],
  ["orientation", value => ({ type: "deviceProfile", profileId: "desktop", orientation: value }), "未知浏览器设备方向"],
  ["settings-orientation", value => ({ type: "deviceSettings", settings: { orientation: value } }), "未知浏览器设备方向"],
  ["panel", value => ({ type: "debugSnapshot", panel: value }), "未知 debugSnapshot.panel"],
  ["selection-mode", value => ({ type: "selectElement", x: 1, y: 2, mode: value }), "未知元素选择模式"],
  ["command", value => ({ type: "command", name: value }), "未知 command name"],
];

describe.each([
  "/private/ws-profile", String.raw`C:\private\ws-profile`, String.raw`\\host\share\ws-profile`,
])("WebSocket拒绝值只进入诊断: %s", nativePath => {
  test.each(unknownFields)("字段%s保留明确拒绝且不回显输入", (_field, message, expected) => {
    const logged = spyOn(process.stderr, "write").mockImplementation(() => undefined);
    try {
      let rejected;
      try { parseClientMessage(JSON.stringify({ browserId: "browser_protocol_test", ...message(nativePath) })); }
      catch (error) { rejected = error; }
      expect(rejected.constructor).toBe(Error);
      expect(rejected.message).toBe(expected);
      expect(logged.mock.calls.map(args => args[0]).join("\n")).toContain(inspect(nativePath));
    } finally {
      logged.mockRestore();
    }
  });

  test("非法JSON保留原始SyntaxError与安全阶段说明", () => {
    const logged = spyOn(process.stderr, "write").mockImplementation(() => undefined);
    try {
      let rejected;
      try { parseClientMessage(Buffer.from(nativePath)); } catch (error) { rejected = error; }
      expect(rejected.constructor).toBe(SyntaxError);
      expect(rejected.message).toBe("WebSocket JSON 请求解析失败: stage=websocket_json, error=SyntaxError");
      expect(rejected.cause).toBeInstanceOf(SyntaxError);
      let original;
      try { JSON.parse(nativePath); } catch (error) { original = error; }
      expect(rejected.cause.message).toBe(original.message);
      expect(logged.mock.calls.map(args => args[0]).join("\n")).toContain(inspect(rejected.cause));
    } finally {
      logged.mockRestore();
    }
  });
});

test("合法页面URL和普通用户正文仍沿原schema规范化", () => {
  const url = "file:///private/user-opened.html";
  expect(parseClientMessage(JSON.stringify({ type: "command", browserId: "browser_native", name: "goto", url })).url).toBe(url);
  const text = String.raw`用户正文 /private/x C:\private\x \\host\share\x`;
  expect(parseClientMessage(JSON.stringify({ type: "paste", browserId: "browser_native", text })).text).toBe(text);
});

test("字段诊断失败保留原拒绝与日志异常", () => {
  const sinkError = new Error("/private/log-sink");
  const logged = spyOn(process.stderr, "write").mockImplementation(() => { throw sinkError; });
  try {
    expect(() => parseClientMessage('{"type":"/private/ws-profile"}')).toThrow(
      expect.objectContaining({ message: "未知消息类型", cause: sinkError }),
    );
  } finally {
    logged.mockRestore();
  }
});
