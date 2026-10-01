import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import WorkspaceFileTree from "./WorkspaceFileTree";
import {
  WORKSPACE_FILE_CHANGES_EVENT,
  type WorkspaceFileChangesEventDetail,
} from "../../../state/workspaceFileTreeEvents";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

// 打开右键菜单会挂载 AnchoredOverlay；@floating-ui 用 `instanceof Element` 判定
// 引用，测试环境没有这些全局，补最小构造器以免在被动副作用里抛 ReferenceError。
class OverlayElementStub {}
const OVERLAY_GLOBALS = { Element: OverlayElementStub, Node: OverlayElementStub } as const;
const originalOverlayGlobals = Object.fromEntries(
  Object.keys(OVERLAY_GLOBALS).map((name) => [
    name,
    Object.getOwnPropertyDescriptor(globalThis, name),
  ]),
);

const mountedRenderers: ReactTestRenderer[] = [];

type Listener = (event: Event) => void;

interface DocumentedWindow {
  dispatch: (event: Event) => void;
}

function installWindow(port: number, promptValue: string | null = null): DocumentedWindow {
  const listeners = new Map<string, Set<Listener>>();
  const dispatch = (event: Event) => {
    for (const listener of listeners.get(event.type) ?? []) {
      listener(event);
    }
  };
  for (const [name, value] of Object.entries(OVERLAY_GLOBALS)) {
    Object.defineProperty(globalThis, name, { configurable: true, value });
  }
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      prompt: () => promptValue,
      ...OVERLAY_GLOBALS,
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
  return { dispatch };
}

function apiResponse(data: unknown): Response {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: "request-file-tree-convergence-test",
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
  listingRequests: string[];
  directoryItems: Map<string, Array<Record<string, unknown>>>;
}

/** 每次 GET /workspace/files 都按 directoryItems 的当前快照返回，便于断言重载次数与被替换内容。 */
async function mountTree(port: number, promptValue: string | null = null): Promise<Harness> {
  const windowHandle = installWindow(port, promptValue);
  const listingRequests: string[] = [];
  const directoryItems = new Map<string, Array<Record<string, unknown>>>([
    ["", [fileNode("src", "directory"), fileNode("other", "directory")]],
    ["src", [fileNode("src/old.ts", "file")]],
    ["other", [fileNode("other/b.ts", "file")]],
  ]);
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
    if (url.pathname === "/api/gateway/auth/local-credential") {
      return apiResponse({ token: "token" });
    }
    if (url.pathname === "/api/gateway/users/current") {
      return apiResponse({ kind: "guest", user_id: null });
    }
    if (url.pathname === "/api/v1/workspace/files") {
      const path = url.searchParams.get("path") ?? "";
      listingRequests.push(path);
      return apiResponse({
        root_path: path,
        path,
        items: directoryItems.get(path) ?? [],
        truncated: false,
        next_cursor: null,
      });
    }
    if (url.pathname === "/api/v1/workspace/files/entries") {
      const path = url.searchParams.get("path") ?? "";
      const next = [...(directoryItems.get(path) ?? []), fileNode(`${path}/new.ts`, "file")];
      directoryItems.set(path, next);
      return apiResponse({ root_path: path, path, items: next, truncated: false, next_cursor: null });
    }
    throw new Error("未声明请求: " + url.pathname);
  }, { preconnect: originalFetch.preconnect });

  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <WorkspaceFileTree
        active
        apiPort={port}
        workspaceId="gw_conv"
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
  return { renderer, window: windowHandle, listingRequests, directoryItems };
}

function titlesOf(renderer: ReactTestRenderer): string[] {
  return renderer.root
    .findAll((node) => typeof node.props?.title === "string")
    .map((node) => String(node.props.title));
}

async function settle(ms = 30): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

function findButton(renderer: ReactTestRenderer, title: string) {
  return renderer.root.find(
    (node) => typeof node.props?.onClick === "function" && node.props.title === title,
  );
}

function expand(renderer: ReactTestRenderer, title: string): void {
  const target = findButton(renderer, title);
  act(() => {
    target.props.onClick();
  });
}

function openMenu(renderer: ReactTestRenderer, title: string): void {
  const target = findButton(renderer, title);
  act(() => {
    target.props.onContextMenu({ preventDefault: () => {}, clientX: 5, clientY: 6 });
  });
}

function clickLabel(renderer: ReactTestRenderer, label: string): void {
  const target = renderer.root.find(
    (node) => typeof node.props?.onClick === "function"
      && node.findAll((child) => child.children?.includes(label)).length > 0,
  );
  act(() => {
    target.props.onClick();
  });
}

function dispatchChange(handle: Harness, kind: string, absolutePath: string): void {
  act(() => {
    handle.window.dispatch(new CustomEvent<WorkspaceFileChangesEventDetail>(
      WORKSPACE_FILE_CHANGES_EVENT,
      { detail: { workspaceId: "gw_conv", changes: [{ kind, path: absolutePath }] } },
    ));
  });
}

function requestCount(handle: Harness, path: string): number {
  return handle.listingRequests.filter((requested) => requested === path).length;
}

afterEach(() => {
  act(() => {
    mountedRenderers.splice(0).forEach((renderer) => renderer.unmount());
  });
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  for (const [name, descriptor] of Object.entries(originalOverlayGlobals)) {
    restoreGlobalDescriptor(name, descriptor);
  }
});

describe("工作区文件树精确收敛", () => {
  test("单目录写操作成功后只替换该目录且不触发任何目录重拉", async () => {
    const port = 49_901;
    const handle = await mountTree(port, "new.ts");
    await settle();
    expand(handle.renderer, "/w/project/src");
    await settle();
    expand(handle.renderer, "/w/project/other");
    await settle();

    // 展开 src 与 other 各一次目录读取，根目录一次。
    expect(requestCount(handle, "src")).toBe(1);
    expect(requestCount(handle, "other")).toBe(1);
    expect(requestCount(handle, "")).toBe(1);

    openMenu(handle.renderer, "/w/project/src");
    await settle();
    clickLabel(handle.renderer, "新建文件");
    await settle(60);

    // 成功路径用后端返回的完整目录对象替换该目录：不得再重拉任何目录。
    expect(requestCount(handle, "src")).toBe(1);
    expect(requestCount(handle, "other")).toBe(1);
    expect(requestCount(handle, "")).toBe(1);
    expect(handle.listingRequests).toHaveLength(3);
    expect(titlesOf(handle.renderer)).toContain("/w/project/src/new.ts");
  });

  test("外部变更 SSE 按受影响父目录收敛，不触发整树重载", async () => {
    const port = 49_902;
    const handle = await mountTree(port);
    await settle();
    expand(handle.renderer, "/w/project/src");
    await settle();
    expand(handle.renderer, "/w/project/other");
    await settle();
    expect(requestCount(handle, "src")).toBe(1);
    expect(requestCount(handle, "other")).toBe(1);

    // 让下次读取 src 时返回新增条目，复现外部（Agent/终端）写入。
    handle.directoryItems.set("src", [
      fileNode("src/old.ts", "file"),
      fileNode("src/added.ts", "file"),
    ]);
    dispatchChange(handle, "create", "/w/project/src/added.ts");
    await settle(320);

    // 只有受影响父目录被重取一次；根目录与兄弟目录不受影响。
    expect(requestCount(handle, "src")).toBe(2);
    expect(requestCount(handle, "other")).toBe(1);
    expect(requestCount(handle, "")).toBe(1);
    expect(handle.listingRequests).toHaveLength(4);
    expect(titlesOf(handle.renderer)).toContain("/w/project/src/added.ts");
  });
});
