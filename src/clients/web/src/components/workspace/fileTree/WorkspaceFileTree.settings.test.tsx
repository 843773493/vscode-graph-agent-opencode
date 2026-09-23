import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import WorkspaceFileTree from "./WorkspaceFileTree";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

const mountedRenderers: ReactTestRenderer[] = [];

type Listener = (event: Event) => void;

function installWindow(port: number): void {
  const listeners = new Map<string, Set<Listener>>();
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
}

function apiResponse(data: unknown): Response {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: "request-file-tree-settings-test",
    data,
  });
}

interface Harness {
  renderer: ReactTestRenderer;
  settingsRequests: number;
  statuses: string[];
  failSettings: boolean;
}

async function settle(ms = 30): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

/** 挂载文件树：快捷路径设置接口始终 500，用于复现「永久加载态」。 */
async function mountTree(port: number, failSettings = true): Promise<Harness> {
  installWindow(port);
  const handle: Harness = {
    renderer: undefined as unknown as ReactTestRenderer,
    settingsRequests: 0,
    statuses: [],
    failSettings,
  };
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
    if (url.pathname === "/api/gateway/auth/local-credential") {
      return apiResponse({ token: "token" });
    }
    if (url.pathname === "/api/gateway/users/current") {
      return apiResponse({ kind: "guest", user_id: null });
    }
    if (url.pathname.endsWith("/file-tree-settings")) {
      handle.settingsRequests += 1;
      if (handle.failSettings) {
        return new Response(JSON.stringify({ detail: "会话存在性校验失败" }), {
          status: 500,
          statusText: "Internal Server Error",
          headers: { "content-type": "application/json" },
        });
      }
      return apiResponse({
        session_id: "ses_settings",
        session_shortcuts: [],
        workspace_shortcuts: [],
        default_shortcuts: [],
        effective_shortcuts: [],
      });
    }
    if (url.pathname === "/api/v1/workspace/files") {
      return apiResponse({
        root_path: url.searchParams.get("path") ?? "",
        path: url.searchParams.get("path") ?? "",
        items: [],
        truncated: false,
        next_cursor: null,
      });
    }
    throw new Error("未声明请求: " + url.pathname);
  }, { preconnect: originalFetch.preconnect });

  await act(async () => {
    handle.renderer = create(
      <WorkspaceFileTree
        active
        apiPort={port}
        workspaceId="gw_settings"
        workspaceName="project"
        workspaceRoot="/w/project"
        sessionId="ses_settings"
        activeFilePath={null}
        searchOpen={false}
        collapseVersion={0}
        expandedPaths={[""]}
        onExpandedPathsChange={() => {}}
        onCloseSearch={() => {}}
        onOpenFile={() => {}}
        onStatusChange={(text) => handle.statuses.push(text)}
      />,
    );
  });
  mountedRenderers.push(handle.renderer);
  return handle;
}

function treeBusy(renderer: ReactTestRenderer): boolean {
  return Boolean(renderer.root.findByProps({ role: "tree" }).props["aria-busy"]);
}

function settingsErrorCards(renderer: ReactTestRenderer): number {
  return renderer.root.findAll(
    (node) => typeof node.props?.className === "string"
      && node.props.className.split(" ").includes("files-tree-settings-error"),
  ).length;
}

afterEach(() => {
  act(() => {
    mountedRenderers.splice(0).forEach((renderer) => renderer.unmount());
  });
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

describe("工作区文件树快捷路径设置读取失败", () => {
  test("设置接口失败后不得永久停在加载态，且必须给出可见错误与重试入口", async () => {
    const handle = await mountTree(49_801);
    await settle(400);

    expect(handle.settingsRequests).toBe(1);
    // 失败必须落在状态栏之外的可见出口：文件树自身要出错误卡，而不是继续转圈。
    expect(handle.statuses.some((text) => text.includes("快捷路径加载失败"))).toBe(true);
    expect(treeBusy(handle.renderer)).toBe(false);
    expect(settingsErrorCards(handle.renderer)).toBe(1);
  });

  test("点击重试后重新读取设置并恢复正常渲染", async () => {
    const handle = await mountTree(49_802);
    await settle(400);
    handle.failSettings = false;

    act(() => {
      handle.renderer.root
        .findByProps({ className: "files-tree-settings-retry" })
        .props.onClick();
    });
    await settle(400);

    expect(handle.settingsRequests).toBe(2);
    expect(treeBusy(handle.renderer)).toBe(false);
    expect(settingsErrorCards(handle.renderer)).toBe(0);
  });
});
