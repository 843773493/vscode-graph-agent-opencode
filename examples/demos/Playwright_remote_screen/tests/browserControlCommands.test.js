import path from "node:path";
import { pathToFileURL } from "node:url";
import { describe, expect, test } from "bun:test";
import { parseBrowserControlCommand } from "../src/server/browserControlCommands.js";
import { normalizeHttpUrl } from "../src/server/url.js";

describe("browser control commands", () => {
  test("为裸域名补齐 https", () => {
    expect(normalizeHttpUrl("example.com")).toBe("https://example.com/");
  });

  test("保留 http URL", () => {
    expect(normalizeHttpUrl("http://127.0.0.1:8130/a")).toBe("http://127.0.0.1:8130/a");
  });

  test("拒绝非 http/https URL", () => {
    const localFileUrl = pathToFileURL(path.resolve("demo.html")).href;
    expect(() => normalizeHttpUrl(localFileUrl)).toThrow("只支持 http/https URL");
  });

  test("解析 goto 命令", () => {
    expect(parseBrowserControlCommand("goto example.com")).toEqual({
      name: "goto",
      url: "https://example.com/",
    });
  });

  test("解析 viewport 命令", () => {
    expect(parseBrowserControlCommand("viewport 1440x900")).toEqual({
      name: "viewport",
      width: 1440,
      height: 900,
    });
  });

  test("解析 click 命令", () => {
    expect(parseBrowserControlCommand("click 10 20")).toEqual({
      name: "click",
      x: 10,
      y: 20,
    });
  });

  test("拒绝未知命令", () => {
    expect(() => parseBrowserControlCommand("dance")).toThrow("未知终端命令");
  });
});
