import { describe, expect, test } from "bun:test";
import { parseTerminalCommand } from "../src/server/terminalCommands.js";
import { normalizeHttpUrl } from "../src/server/url.js";

describe("terminal commands", () => {
  test("为裸域名补齐 https", () => {
    expect(normalizeHttpUrl("example.com")).toBe("https://example.com/");
  });

  test("保留 http URL", () => {
    expect(normalizeHttpUrl("http://127.0.0.1:8130/a")).toBe("http://127.0.0.1:8130/a");
  });

  test("拒绝非 http/https URL", () => {
    expect(() => normalizeHttpUrl("file:///tmp/demo.html")).toThrow("只支持 http/https URL");
  });

  test("解析 goto 命令", () => {
    expect(parseTerminalCommand("goto example.com")).toEqual({
      name: "goto",
      url: "https://example.com/",
    });
  });

  test("解析 viewport 命令", () => {
    expect(parseTerminalCommand("viewport 1440x900")).toEqual({
      name: "viewport",
      width: 1440,
      height: 900,
    });
  });

  test("解析 click 命令", () => {
    expect(parseTerminalCommand("click 10 20")).toEqual({
      name: "click",
      x: 10,
      y: 20,
    });
  });

  test("拒绝未知命令", () => {
    expect(() => parseTerminalCommand("dance")).toThrow("未知终端命令");
  });
});
