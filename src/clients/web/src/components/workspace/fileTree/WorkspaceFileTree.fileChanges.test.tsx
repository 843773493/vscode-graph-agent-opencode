import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import WorkspaceFileTree from "./WorkspaceFileTree";
import {
  WORKSPACE_FILE_CHANGES_EVENT,
  type WorkspaceFileChangesEventDetail,
} from "../../../state/workspaceFileTreeEvents";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

// 组件挂载会启动文件监听重连定时器；这里统一记录以便在恢复 window 之前卸载，
// 否则迟到的重连回调会在 window 被移除后抛 ReferenceError 并污染其他测试文件。
const mountedRenderers: ReactTestRenderer[] = [];

type Listener = (event: Event) => void;

interface DocumentedWindow {
  listeners: Map<string, Set<Listener>>;
  dispatch: (event: Event) => void;
}

// 文件变更 effect 依赖 window 上的事件绑定与定时器；这里给出可派发的最小实现。
function installWindow(port: number): DocumentedWindow {
  const listeners = new Map<string, Set<Listener>>();
  const dispatch = (event: Event) => {
    for (const listener of listeners.get(event.type) ?? []) {
      listener(event);
    }
  };
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: (type: string, listener: Listener) => {
        const bucket = listeners.get(type) ?? new Set<Listener>();
        bucket.add(listener);
        listeners.set(type, bucket);
      },
      removeEventListener: (type: string, listener: Listener) => {
        listeners.get(type)?.delete(listener);
      },
    },
  });
  return { listeners, dispatch };
}

function apiResponse(data: unknown): Response {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: "request-workspace-file-tree-changes-test",
    data,
  });
}

function fileNode(path: string, kind: "file" | "directory"): Record<string, unknown> {
  return {
    name: path.split("/").pop() ?? path,
    path,
    kind,
    has_children: kind === "directory",
    size: kind === "file" ? 1 : null,
    modified_at: null,
  };
}

interface Harness {
  renderer: ReactTestRenderer;
  window: DocumentedWindow;
  releaseDeferred: () => void;
  deferredPaths: string[];
  deletedPaths: Set<string>;
  listingRequests: string[];
  listingSignals: Map<string, AbortSignal | null>;
}

async function mountTree(port: number): Promise<Harness> {
  const windowHandle = installWindow(port);
  const deferred: Array<() => void> = [];
  const deferredPaths: string[] = [];
  // 后端在删除提交后不再列出该条目；列表请求据此复现真实返回。
  const deletedPaths = new Set<string>();
  const listingRequests: string[] = [];
  const listingSignals = new Map<string, AbortSignal | null>();
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
    if (url.pathname === "/api/gateway/auth/local-credential") {
      return apiResponse({ token: "token" });
    }
    if (url.pathname === "/api/gateway/users/current") {
      return apiResponse({ kind: "guest", user_id: null });
    }
    if (url.pathname !== "/api/v1/workspace/files") {
      throw new Error("未声明请求: " + url.pathname);
    }
    const path = url.searchParams.get("path") ?? "";
    listingRequests.push(path);
    listingSignals.set(
      path,
      (input instanceof Request ? input.signal : null) ?? init?.signal ?? null,
    );
    if (path === "src/gone") {
      // 删除被派发之后这个响应才会到达，用来证明它不会把已删除目录写回缓存。
      deferredPaths.push(path);
      await new Promise<void>((resolve) => { deferred.push(resolve); });
    }
    const items: Array<Record<string, unknown>> = (
      path === ""
        ? [fileNode("src", "directory")]
        : path === "src"
          ? [fileNode("src/gone", "directory"), fileNode("src/kept", "directory")]
          : [fileNode(path + "/a.ts", "file")]
    ).filter((item) => !deletedPaths.has(String(item.path)));
    return apiResponse({ root_path: path, path, items, truncated: false, next_cursor: null });
  }, { preconnect: originalFetch.preconnect });

  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <WorkspaceFileTree
        active
        apiPort={port}
        workspaceId="gw_changes"
        workspaceName="project"
        workspaceRoot="/w/project"
        sessionId=""
        activeFilePath={null}
        searchOpen={false}
        collapseVersion={0}
        expandedPaths={[""]}
        onExpandedPathsChange={() => {}}
        onCloseSearch={() => {}}
        onOpenFile={() => {}}
        onStatusChange={() => {}}
      />,
    );
  });
  mountedRenderers.push(renderer);
  return {
    renderer,
    window: windowHandle,
    releaseDeferred: () => { deferred.splice(0).forEach((resolve) => resolve()); },
    deferredPaths,
    deletedPaths,
    listingRequests,
    listingSignals,
  };
}

function titlesOf(renderer: ReactTestRenderer): string[] {
  return renderer.root
    .findAll((node) => typeof node.props?.onClick === "function")
    .map((node) => String(node.props.title));
}

async function settle(ms = 30): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

function clickByTitle(renderer: ReactTestRenderer, title: string): void {
  const target = renderer.root.find(
    (node) => typeof node.props?.onClick === "function" && node.props.title === title,
  );
  act(() => {
    target.props.onClick();
  });
}

function dispatchDelete(handle: Harness, absolutePath: string): void {
  act(() => {
    handle.window.dispatch(new CustomEvent<WorkspaceFileChangesEventDetail>(
      WORKSPACE_FILE_CHANGES_EVENT,
      { detail: { workspaceId: "gw_changes", changes: [{ kind: "delete", path: absolutePath }] } },
    ));
  });
  handle.deletedPaths.add(absolutePath.replace("/w/project/", ""));
}

afterEach(() => {
  act(() => {
    mountedRenderers.splice(0).forEach((renderer) => renderer.unmount());
  });
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("工作区文件树文件变更边界", () => {
  test("删除展开中的目录后迟到响应不再把它渲染回来", async () => {
    const port = 49_701;
    const handle = await mountTree(port);
    await settle();
    clickByTitle(handle.renderer, "/w/project/src");
    await settle();
    expect(titlesOf(handle.renderer)).toContain("/w/project/src/gone");

    // 展开 gone 会发起请求，但该请求在删除派发之后才会返回。
    clickByTitle(handle.renderer, "/w/project/src/gone");
    await settle();
    expect(handle.deferredPaths).toEqual(["src/gone"]);

    dispatchDelete(handle, "/w/project/src/gone");
    await settle(300);
    expect(titlesOf(handle.renderer)).not.toContain("/w/project/src/gone");
    // 删除必须真正中止在途请求，而不是等它返回后再丢弃：请求所用的
    // AbortSignal 必须在删除派发后已经进入 aborted 状态。
    const inFlightSignal = handle.listingSignals.get("src/gone");
    expect(inFlightSignal?.aborted).toBe(true);

    handle.releaseDeferred();
    await settle(50);
    expect(titlesOf(handle.renderer)).not.toContain("/w/project/src/gone");
    expect(titlesOf(handle.renderer)).toContain("/w/project/src/kept");
  });

  test("删除子目录后父级强制重新读取后端，被删除的子目录不再渲染", async () => {
    const port = 49_702;
    const handle = await mountTree(port);
    await settle();
    clickByTitle(handle.renderer, "/w/project/src");
    await settle();
    expect(titlesOf(handle.renderer)).toContain("/w/project/src/kept");
    dispatchDelete(handle, "/w/project/src/kept");
    await settle(300);
    expect(handle.deletedPaths.has("src/kept")).toBe(true);
    expect(handle.listingRequests.filter((path) => path === "src").length)
      .toBeGreaterThan(1);
    expect(titlesOf(handle.renderer)).not.toContain("/w/project/src/kept");
    expect(titlesOf(handle.renderer)).toContain("/w/project/src/gone");
  });
});
