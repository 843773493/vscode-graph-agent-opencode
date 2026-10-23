import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useRef } from "react";
import { useWorkspaceFileTreeDirectories } from "./useWorkspaceFileTreeDirectories";
import { fileTreeNodeFixture as fileNode } from "./workspaceFileTreeTestFixtures";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const workspaceRootUri = "boxteam://workspace/ws-1/resources/files/root";
const treePath = (path: string): string => path ? `${workspaceRootUri}/${path}` : workspaceRootUri;

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
  if (data && typeof data === "object" && "items" in data && "path" in data) {
    data = { directory_key: { workspace_id: "ws-1", scope: "workspace", root: workspaceRootUri, path: data.path },
      directory_revision: 1, ...data };
  }
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

interface HarnessHandle {
  loadDirectory: (path: string, force?: boolean, append?: boolean) => Promise<boolean>;
  directoriesRef: { current: Record<string, unknown> };
  reloadExpandedDirectories: (roots: readonly string[]) => void;
  invalidateDirectoriesUnder: (treePath: string) => void;
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
      workspaceId: "ws-1",
      expandedPathsRef,
      shortcutTreePathsRef,
      activeFilePathRef,
      onStatusChange: (text: string) => statuses.push(text),
    });
    handle.loadDirectory = api.loadDirectory;
    handle.directoriesRef = api.directoriesRef;
    handle.reloadExpandedDirectories = api.reloadExpandedDirectories;
    handle.invalidateDirectoriesUnder = api.invalidateDirectoriesUnder;
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
  restoreGlobalDescriptor("window", originalWindowDescriptor);
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
          root_path: workspaceRootUri,
          path: url.searchParams.get("path") ?? "",
          items: [fileNode(treePath("src/a.ts"))],
          truncated: false,
          next_cursor: null,
        });
      }
      throw new Error(`未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      const results = await Promise.all([
        handle.loadDirectory(treePath("src")),
        handle.loadDirectory(treePath("src")),
      ]);
      expect(results).toEqual([true, true]);
    });
    expect(requests).toBe(1);
    expect(handle.directoriesRef.current[treePath("src")]).toMatchObject({
      loading: false,
      error: null,
      items: [{ path: treePath("src/a.ts") }],
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
            root_path: workspaceRootUri, path: treePath("src"),
            items: [fileNode(treePath("src/a.ts"))],
            truncated: false, next_cursor: "cursor-1",
          });
        }
        return apiResponse({
          root_path: workspaceRootUri, path: treePath("src"),
          items: [fileNode(treePath("src/a.ts")), fileNode(treePath("src/b.ts"))],
          truncated: false, next_cursor: null,
        });
      }
      throw new Error(`未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      await handle.loadDirectory(treePath("src"));
    });
    expect(handle.directoriesRef.current[treePath("src")]).toMatchObject({ nextCursor: "cursor-1" });
    await act(async () => {
      await handle.loadDirectory(treePath("src"), false, true);
    });
    const items = (handle.directoriesRef.current[treePath("src")] as { items: Array<{ path: string }> }).items;
    expect(items.map((item) => item.path)).toEqual([treePath("src/a.ts"), treePath("src/b.ts")]);
    expect(handle.directoriesRef.current[treePath("src")]).toMatchObject({ nextCursor: null });
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
      return apiResponse({ root_path: workspaceRootUri, path: workspaceRootUri, items: [], truncated: false, next_cursor: null });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    let result = false;
    await act(async () => {
      result = await handle.loadDirectory(treePath("src"), false, true);
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
      result = await handle.loadDirectory(treePath("src"));
    });
    expect(result).toBe(false);
    expect(handle.directoriesRef.current[treePath("src")]).toMatchObject({
      loading: false,
      error: "请求失败 500 Internal Server Error: 目录读取失败",
    });
    expect(statuses.some((text) => text.startsWith("文件树加载失败: "))).toBe(true);
  });

  test("全树重同步丢弃缓存并只重取已有缓存的展开路径", async () => {
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
        root_path: workspaceRootUri, path,
        items: [fileNode(treePath("src/a.ts"))],
        truncated: false, next_cursor: null,
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      await handle.loadDirectory(treePath("src"));
      await handle.loadDirectory(treePath("other"));
    });
    await act(async () => {
      // ghost 已展开但从未加载过，不得因为它被展开就补发请求。
      handle.commitExpanded([treePath("src"), treePath("ghost")]);
      handle.reloadExpandedDirectories([workspaceRootUri]);
      await Promise.resolve();
    });
    expect(requestedPaths.filter((path) => path === treePath("src")).length).toBe(2);
    expect(requestedPaths.filter((path) => path === treePath("other")).length).toBe(1);
    expect(requestedPaths).not.toContain(treePath("ghost"));
    // other 未处于展开态：重同步只丢弃它的缓存，不得补发请求。
    expect(handle.directoriesRef.current[treePath("other")]).toBeUndefined();
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
        root_path: workspaceRootUri, path: treePath("src"),
        items: [fileNode(treePath("src/late.ts"))],
        truncated: false, next_cursor: "late-cursor",
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    let pending!: Promise<boolean>;
    await act(async () => {
      pending = handle.loadDirectory(treePath("src"));
      await Promise.resolve();
    });
    expect(handle.directoriesRef.current[treePath("src")]).toMatchObject({ loading: true });
    let abortedPaths: string[] = [];
    act(() => {
      abortedPaths = handle.abortAllDirectoryRequests();
    });
    expect(abortedPaths).toEqual([treePath("src")]);
    releaseList.current?.();
    let resolved = true;
    await act(async () => {
      resolved = await pending;
    });
    expect(resolved).toBe(false);
    expect(handle.directoriesRef.current[treePath("src")]).toMatchObject({
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
      await handle.loadDirectory(workspaceRootUri);
    });
    const entry = handle.directoriesRef.current[workspaceRootUri] as { error?: string };
    expect(entry.error).toContain("没有访问权限");
    expect(entry.error).toContain("请求失败 403 Forbidden");
    expect(entry.error).toContain("文件树路径无访问权限: root");
    expect(statuses.some((text) =>
      text.startsWith("文件树加载失败: ") && text.includes("没有访问权限"))).toBe(true);
  });

  test("子树失效后加载中被删除的目录不再回填迟到响应", async () => {
    const port = 49_613;
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
      const path = url.searchParams.get("path") ?? "";
      return apiResponse({
        root_path: workspaceRootUri, path,
        items: [fileNode(path + "/stale.ts")],
        truncated: false, next_cursor: null,
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    let pending!: Promise<boolean>;
    await act(async () => {
      pending = handle.loadDirectory(treePath("src/gone"));
      await Promise.resolve();
    });
    expect(handle.directoriesRef.current[treePath("src/gone")]).toMatchObject({ loading: true });
    act(() => {
      handle.invalidateDirectoriesUnder(treePath("src"));
    });
    releaseList.current?.();
    let resolved = true;
    await act(async () => {
      resolved = await pending;
    });
    expect(resolved).toBe(false);
    expect(handle.directoriesRef.current[treePath("src/gone")]).toBeUndefined();
  });

  test("子树失效只丢弃该路径及其后代，兄弟与父级缓存保留", async () => {
    const port = 49_614;
    installWindow(port);
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      const path = url.searchParams.get("path") ?? "";
      return apiResponse({
        root_path: workspaceRootUri, path,
        items: [fileNode(path + "/a.ts")],
        truncated: false, next_cursor: null,
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      await handle.loadDirectory(treePath("src"));
      await handle.loadDirectory(treePath("src/gone"));
      await handle.loadDirectory(treePath("src/kept"));
      await handle.loadDirectory(treePath("other"));
    });
    act(() => {
      handle.invalidateDirectoriesUnder(treePath("src/gone"));
    });
    expect(handle.directoriesRef.current[treePath("src/gone")]).toBeUndefined();
    expect(handle.directoriesRef.current[treePath("src")]).toBeDefined();
    expect(handle.directoriesRef.current[treePath("src/kept")]).toBeDefined();
    expect(handle.directoriesRef.current[treePath("other")]).toBeDefined();
  });

  test("失效后重新加载能拿到后端最新子项，不被旧缓存污染", async () => {
    const port = 49_615;
    installWindow(port);
    let items = [fileNode(treePath("src/old.ts"))];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      const path = url.searchParams.get("path") ?? "";
      return apiResponse({
        root_path: workspaceRootUri, path,
        items,
        truncated: false, next_cursor: null,
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    await act(async () => {
      await handle.loadDirectory(treePath("src"));
    });
    items = [fileNode(treePath("src/new.ts"))];
    act(() => {
      handle.invalidateDirectoriesUnder(treePath("src"));
    });
    await act(async () => {
      await handle.loadDirectory(treePath("src"));
    });
    const entry = handle.directoriesRef.current[treePath("src")] as { items: Array<{ path: string }> };
    expect(entry.items.map((item) => item.path)).toEqual([treePath("src/new.ts")]);
  });

  test("失效会让叶子路径的在途请求结果一并作废", async () => {
    const port = 49_616;
    installWindow(port);
    const releases: Array<() => void> = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "token" });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      await new Promise<void>((resolve) => { releases.push(resolve); });
      const path = url.searchParams.get("path") ?? "";
      return apiResponse({
        root_path: workspaceRootUri, path,
        items: [fileNode(path + "/a.ts")],
        truncated: false, next_cursor: null,
      });
    }, { preconnect: originalFetch.preconnect });

    const { handle } = mountHarness(port);
    let first!: Promise<boolean>;
    await act(async () => {
      first = handle.loadDirectory(treePath("src/gone"));
      await Promise.resolve();
    });
    act(() => {
      handle.invalidateDirectoriesUnder(treePath("src/gone"));
    });
    expect(handle.directoriesRef.current[treePath("src/gone")]).toBeUndefined();
    let second!: Promise<boolean>;
    await act(async () => {
      second = handle.loadDirectory(treePath("src/gone"));
      await Promise.resolve();
    });
    expect(handle.directoriesRef.current[treePath("src/gone")]).toMatchObject({ loading: true });
    releases.forEach((release) => release());
    await act(async () => {
      await Promise.all([first, second]);
    });
    expect(handle.directoriesRef.current[treePath("src/gone")]).toMatchObject({ loading: false });
  });

});
