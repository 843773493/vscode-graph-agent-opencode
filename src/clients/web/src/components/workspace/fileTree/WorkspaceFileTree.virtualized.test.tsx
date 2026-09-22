import { afterEach, describe, expect, mock, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";


const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

const mountedRenderers: ReactTestRenderer[] = [];

// 超过阈值时文件树走 react-virtuoso 扁平行路径。测试替换列表容器，
// 直接对每一行调用 itemContent，从而观察扁平行与树形递归行是否同构。
mock.module("react-virtuoso", () => ({
  Virtuoso: (props: {
    data: unknown[];
    itemContent: (index: number, row: unknown) => React.ReactNode;
    computeItemKey: (index: number, row: unknown) => React.Key;
  }) => (
    <>
      {props.data.map((row, index) => (
        <div key={props.computeItemKey(index, row)}>{props.itemContent(index, row)}</div>
      ))}
    </>
  ),
}));

const { default: WorkspaceFileTree } = await import("./WorkspaceFileTree");

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: () => {},
      removeEventListener: () => {},
    },
  });
}

function apiResponse(data: unknown): Response {
  return Response.json({ code: 0, message: "ok", request_id: "req", data });
}

function fileNode(index: number): Record<string, unknown> {
  return {
    name: "file-" + index + ".ts",
    path: "file-" + index + ".ts",
    kind: "file",
    has_children: false,
    size: index,
    modified_at: null,
  };
}

function directoryNode(path: string): Record<string, unknown> {
  return {
    name: path.split("/").pop() ?? path,
    path,
    kind: "directory",
    has_children: true,
    size: null,
    modified_at: null,
  };
}

async function mount(port: number, activeFilePath: string | null, expanded: string[]): Promise<ReactTestRenderer> {
  installWindow(port);
  const items = [directoryNode("big"), ...Array.from({ length: 320 }, (_, i) => fileNode(i))];
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
    if (url.pathname === "/api/gateway/auth/local-credential") return apiResponse({ token: "t" });
    if (url.pathname === "/api/gateway/users/current") return apiResponse({ kind: "guest", user_id: null });
    const path = url.searchParams.get("path") ?? "";
    return apiResponse({
      root_path: path,
      path,
      items: path === "" ? items : [fileNode(999)],
      truncated: false,
      next_cursor: null,
    });
  }, { preconnect: originalFetch.preconnect });
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <WorkspaceFileTree
        active
        apiPort={port}
        workspaceId="gw_virt"
        workspaceName="project"
        workspaceRoot="/w/project"
        sessionId=""
        activeFilePath={activeFilePath}
        searchOpen={false}
        collapseVersion={0}
        expandedPaths={expanded}
        onExpandedPathsChange={() => {}}
        onCloseSearch={() => {}}
        onOpenFile={() => {}}
        onStatusChange={() => {}}
      />,
    );
  });
  mountedRenderers.push(renderer);
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
  return renderer;
}

function rowFor(renderer: ReactTestRenderer, title: string) {
  return renderer.root.findAll(
    (node) => typeof node.props?.onClick === "function" && node.props.title === title,
  );
}

function classOf(node: { props: { className?: string } }): string {
  return String(node.props.className ?? "");
}

afterEach(() => {
  act(() => { mountedRenderers.splice(0).forEach((r) => r.unmount()); });
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  else Reflect.deleteProperty(globalThis, "window");
});

describe("工作区文件树虚拟滚动扁平行", () => {
  test("超过阈值时扁平行渲染活动文件与目录折叠标记", async () => {
    const port = 49_801;
    const renderer = await mount(port, "file-7.ts", [""]);
    const activeRow = rowFor(renderer, "/w/project/file-7.ts");
    expect(activeRow).toHaveLength(1);
    expect(classOf(activeRow[0])).toContain("active");
    const chevron = activeRow[0].find((n) => classOf(n).includes("files-tree-chevron"));
    expect(classOf(chevron)).toBe("codicon files-tree-chevron");
  });

  test("展开的目录扁平行带 directory 类与向下箭头", async () => {
    const port = 49_802;
    const renderer = await mount(port, "file-7.ts", ["", "big"]);
    const dirRow = rowFor(renderer, "/w/project/big");
    expect(dirRow).toHaveLength(1);
    expect(classOf(dirRow[0])).toContain("directory");
    const chevron = dirRow[0].find((n) => classOf(n).includes("files-tree-chevron"));
    expect(classOf(chevron)).toContain("codicon-chevron-down");
  });

  test("未展开的目录扁平行带向右箭头", async () => {
    const port = 49_803;
    const renderer = await mount(port, null, [""]);
    const dirRow = rowFor(renderer, "/w/project/big");
    expect(dirRow).toHaveLength(1);
    const chevron = dirRow[0].find((n) => classOf(n).includes("files-tree-chevron"));
    expect(classOf(chevron)).toContain("codicon-chevron-right");
  });

  test("文件行展示字节大小而目录行不展示", async () => {
    const port = 49_804;
    const renderer = await mount(port, null, [""]);
    const fileRow = rowFor(renderer, "/w/project/file-3.ts");
    expect(fileRow[0].findAll((n) => classOf(n) === "files-tree-meta").length).toBe(1);
    const dirRow = rowFor(renderer, "/w/project/big");
    expect(dirRow[0].findAll((n) => classOf(n) === "files-tree-meta").length).toBe(0);
  });
});

