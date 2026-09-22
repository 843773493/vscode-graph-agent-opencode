import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useRef } from "react";
import { useWorkspaceFileTreeDirectories } from "./useWorkspaceFileTreeDirectories";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

function apiResponse(data: unknown): Response {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: "request-file-tree-directories-test",
    data,
  });
}

// 安装了 window 时 API 基础地址为空串，fetch 收到的是同源相对路径，
// 这里统一补一个基准再解析，避免把相对路径当成非法 URL。
function requestUrl(input: RequestInfo | URL): URL {
  return new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
}

function fileNode(path: string): Record<string, unknown> {
  return {
    name: path.split("/").pop() ?? path,
    path,
    kind: "file",
    has_children: false,
    size: 1,
    modified_at: null,
  };
}

interface HarnessHandle {
  loadDirectory: (path: string, force?: boolean, append?: boolean) => Promise<boolean>;
  directoriesRef: { current: Record<string, unknown> };
  refreshExpandedDirectories: () => void;
  abortAllDirectoryRequests: () => string[];
  commitExpanded: (paths: string[]) => void;
}

function mountHarness(port: number): {
  renderer: ReactTestRenderer;
  handle: HarnessHandle;
  statuses: string[];
} {
  const statuses: string[] = [];
  const handle: Partial<HarnessHandle> = {};

  function Harness(): React.ReactNode {
    const expandedPathsRef = useRef<Set<string>>(new Set());
    const shortcutTreePathsRef = useRef<Set<string>>(new Set());
    const activeFilePathRef = useRef<string | null>(null);
    handle.commitExpanded = (paths: string[]) => {
      expandedPathsRef.current = new Set(paths);
    };
    const api = useWorkspaceFileTreeDirectories({
      port,
      workspaceId: "gw_test",
      expandedPathsRef,
      shortcutTreePathsRef,
      activeFilePathRef,
      onStatusChange: (text: string) => statuses.push(text),
    });
    handle.loadDirectory = api.loadDirectory;
    handle.directoriesRef = api.directoriesRef;
    handle.refreshExpandedDirectories = api.refreshExpandedDirectories;
    handle.abortAllDirectoryRequests = api.abortAllDirectoryRequests;
    return null;
  }

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<Harness />);
  });
  return { renderer, handle: handle as HarnessHandle, statuses };
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("workspace 文件树目录缓存", () => {
  test("同一路径并发加载只发起一次请求并共享结果", async () => {
    const port = 49_601;
    installWindow(port);
    let requests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.pathname === "/api/v1/workspace/files") {
        requests += 1;
        await new Promise((resolve) => setTimeout(resolve, 10));
        return apiResponse({
          root_path: url.searchParams.get("path") ?? "",
          path: url.searchParams.get("path") ?? "",
          items: [fileNode("src/a.ts")],
          truncated: false,
          next_cursor: null,
        });
      }
      throw new Error(`未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      const results = await Promise.all([
        handle.loadDirectory("src"),
        handle.loadDirectory("src"),
      ]);
      expect(results).toEqual([true, true]);
    });
    expect(requests).toBe(1);
    expect(handle.directoriesRef.current["src"]).toMatchObject({
      loading: false,
      error: null,
      items: [{ path: "src/a.ts" }],
    });
  });

  test("追加分页按路径合并去重并更新游标", async () => {
    const port = 49_602;
    installWindow(port);
    let page = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.pathname === "/api/v1/workspace/files") {
        page += 1;
        if (page === 1) {
          return apiResponse({
            root_path: "", path: "",
            items: [fileNode("src/a.ts")],
            truncated: false, next_cursor: "cursor-1",
          });
        }
        return apiResponse({
          root_path: "src", path: "src",
          items: [fileNode("src/a.ts"), fileNode("src/b.ts")],
          truncated: false, next_cursor: null,
        });
      }
      throw new Error(`未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      await handle.loadDirectory("src");
    });
    expect(handle.directoriesRef.current["src"]).toMatchObject({ nextCursor: "cursor-1" });
    await act(async () => {
      await handle.loadDirectory("src", false, true);
    });
    const items = (handle.directoriesRef.current["src"] as { items: Array<{ path: string }> }).items;
    expect(items.map((item) => item.path)).toEqual(["src/a.ts", "src/b.ts"]);
    expect(handle.directoriesRef.current["src"]).toMatchObject({ nextCursor: null });
  });

  test("没有下一页游标时追加加载直接短路不发请求", async () => {
    const port = 49_603;
    installWindow(port);
    let requests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      requests += 1;
      return apiResponse({ root_path: "", path: "", items: [], truncated: false, next_cursor: null });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    let result = false;
    await act(async () => {
      result = await handle.loadDirectory("src", false, true);
    });
    expect(result).toBe(true);
    expect(requests).toBe(0);
  });

  test("加载失败写入错误并把消息上报状态栏", async () => {
    const port = 49_604;
    installWindow(port);
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      return new Response(JSON.stringify({ detail: "目录读取失败" }), {
        status: 500,
        statusText: "Internal Server Error",
        headers: { "Content-Type": "application/json" },
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle, statuses } = mountHarness(port);
    let result = true;
    await act(async () => {
      result = await handle.loadDirectory("src");
    });
    expect(result).toBe(false);
    expect(handle.directoriesRef.current["src"]).toMatchObject({
      loading: false,
      error: "请求失败 500 Internal Server Error: 目录读取失败",
    });
    expect(statuses.some((text) => text.startsWith("文件树加载失败: "))).toBe(true);
  });

  test("刷新展开目录把已有条目标记过期并只重载已有缓存的展开路径", async () => {
    const port = 49_605;
    installWindow(port);
    const requestedPaths: string[] = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      const path = url.searchParams.get("path") ?? "";
      requestedPaths.push(path);
      return apiResponse({
        root_path: path, path,
        items: [fileNode("src/a.ts")],
        truncated: false, next_cursor: null,
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      await handle.loadDirectory("src");
      await handle.loadDirectory("other");
    });
    await act(async () => {
      // ghost 已展开但从未加载过，不得因为它被展开就补发请求。
      handle.commitExpanded(["src", "ghost"]);
      handle.refreshExpandedDirectories();
      await Promise.resolve();
    });
    expect(requestedPaths.filter((path) => path === "src").length).toBe(2);
    expect(requestedPaths.filter((path) => path === "other").length).toBe(1);
    expect(requestedPaths).not.toContain("ghost");
  });

  test("中止在途请求后迟到的响应不写入缓存", async () => {
    const port = 49_606;
    installWindow(port);
    const releaseList: { current: (() => void) | null } = { current: null };
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      await new Promise<void>((resolve) => { releaseList.current = resolve; });
      // 迟到的响应带真实条目，用来证明中止后它不会写进缓存。
      return apiResponse({
        root_path: "src", path: "src",
        items: [fileNode("src/late.ts")],
        truncated: false, next_cursor: "late-cursor",
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    let pending!: Promise<boolean>;
    await act(async () => {
      pending = handle.loadDirectory("src");
      await Promise.resolve();
    });
    expect(handle.directoriesRef.current["src"]).toMatchObject({ loading: true });
    let abortedPaths: string[] = [];
    act(() => {
      abortedPaths = handle.abortAllDirectoryRequests();
    });
    expect(abortedPaths).toEqual(["src"]);
    releaseList.current?.();
    let resolved = true;
    await act(async () => {
      resolved = await pending;
    });
    expect(resolved).toBe(false);
    expect(handle.directoriesRef.current["src"]).toMatchObject({
      items: [],
      nextCursor: null,
    });
  });

  test("403 权限失败时目录错误与状态栏都带可执行提示且保留原始文本", async () => {
    const port = 49_612;
    installWindow(port);
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      return new Response(JSON.stringify({ detail: "文件树路径无访问权限: root" }), {
        status: 403,
        statusText: "Forbidden",
        headers: { "Content-Type": "application/json" },
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle, statuses } = mountHarness(port);
    await act(async () => {
      await handle.loadDirectory("root");
    });
    const entry = handle.directoriesRef.current["root"] as { error?: string };
    expect(entry.error).toContain("没有访问权限");
    expect(entry.error).toContain("请求失败 403 Forbidden");
    expect(entry.error).toContain("文件树路径无访问权限: root");
    expect(statuses.some((text) =>
      text.startsWith("文件树加载失败: ") && text.includes("没有访问权限"))).toBe(true);
  });
});
