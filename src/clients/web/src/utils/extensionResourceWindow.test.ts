import { afterEach, describe, expect, test } from "bun:test";
import {
  buildExtensionWindowUrl,
  resolveExtensionWindowRequest,
} from "./extensionResourceWindow";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");

function stubWindow(pathname: string, search: string): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { location: { pathname, search, href: `http://127.0.0.1:8011${pathname}${search}` } },
  });
}

afterEach(() => {
  if (originalWindow) {
    Object.defineProperty(globalThis, "window", originalWindow);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("扩展窗口请求解析", () => {
  test("非扩展窗口路径返回 null", () => {
    stubWindow("/", "?resourceType=browser");
    expect(resolveExtensionWindowRequest()).toBeNull();
  });

  test("/extension 路径识别为扩展请求", () => {
    stubWindow("/extension", "?resourceType=terminal&resourceId=t1");
    expect(resolveExtensionWindowRequest()).toEqual({
      kind: "terminal",
      resourceId: "t1",
      workspaceId: null,
      sessionId: null,
    });
  });

  test("?window=extension 入口识别为扩展请求", () => {
    stubWindow("/", "?window=extension&resourceType=debug");
    expect(resolveExtensionWindowRequest()?.kind).toBe("debug");
  });

  test("缺少 resourceType 但有 browserId 时回落为 browser", () => {
    stubWindow("/extension", "?browserId=b9");
    expect(resolveExtensionWindowRequest()).toEqual({
      kind: "browser",
      resourceId: "b9",
      workspaceId: null,
      sessionId: null,
    });
  });

  test("resourceType 非法时 kind 为 null", () => {
    stubWindow("/extension", "?resourceType=unknown&resourceId=x");
    expect(resolveExtensionWindowRequest()?.kind).toBeNull();
  });
});

describe("扩展窗口 URL 组装", () => {
  test("包含全部资源参数的查询串", () => {
    stubWindow("/", "");
    const raw = buildExtensionWindowUrl({
      kind: "browser",
      resourceId: "b1",
      workspaceId: "w1",
      sessionId: "s1",
    });
    const url = new URL(raw);
    expect(url.pathname).toBe("/extension");
    expect(url.searchParams.get("resourceType")).toBe("browser");
    expect(url.searchParams.get("resourceId")).toBe("b1");
    expect(url.searchParams.get("workspaceId")).toBe("w1");
    expect(url.searchParams.get("sessionId")).toBe("s1");
  });

  test("缺少可选参数时不写入对应查询项", () => {
    stubWindow("/", "");
    const url = new URL(buildExtensionWindowUrl({ kind: "terminal" }));
    expect([...url.searchParams.keys()]).toEqual(["resourceType"]);
    expect(url.searchParams.get("resourceType")).toBe("terminal");
  });
});
