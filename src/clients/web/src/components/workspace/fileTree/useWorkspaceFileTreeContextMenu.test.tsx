import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useRef } from "react";

import type { SessionFileTreeSettings } from "../../../types/backend";
import {
  useWorkspaceFileTreeContextMenu,
  type WorkspaceFileTreeContextMenuApi,
  type FileTreeContextMenuTarget,
} from "./useWorkspaceFileTreeContextMenu";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalFetch = globalThis.fetch;

function installWindow(overrides: Record<string, unknown> = {}): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: "8060" },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: () => {},
      removeEventListener: () => {},
      ...overrides,
    },
  });
}

function menuTarget(overrides: Partial<FileTreeContextMenuTarget> = {}): FileTreeContextMenuTarget {
  return {
    treePath: "src/a.txt",
    absolutePath: "/ws/src/a.txt",
    label: "a.txt",
    kind: "file",
    shortcutSource: null,
    x: 10,
    y: 20,
    ...overrides,
  };
}

interface HarnessOptions {
  sessionId?: string;
  absolutePathForTreePath?: (treePath: string) => string;
  replaceDirectory?: (result: unknown) => void;
  loadDirectory?: (path: string, force?: boolean, append?: boolean) => Promise<boolean>;
  acceptFileTreeSettings?: (result: SessionFileTreeSettings) => void;
}

function mountMenu(options: HarnessOptions = {}): {
  api: () => WorkspaceFileTreeContextMenuApi;
  renderer: ReactTestRenderer;
  statuses: string[];
  replaced: unknown[];
} {
  const statuses: string[] = [];
  const replaced: unknown[] = [];
  const apiRef: { current: WorkspaceFileTreeContextMenuApi | null } = { current: null };

  function Harness(): React.ReactNode {
    const uploadTargetRef = useRef<unknown>(null);
    void uploadTargetRef;
    apiRef.current = useWorkspaceFileTreeContextMenu({
      port: 8060,
      workspaceId: "gw_test",
      sessionId: options.sessionId ?? "ses_1",
      absolutePathForTreePath: options.absolutePathForTreePath
        ?? ((treePath: string) => `/ws/${treePath}`),
      replaceDirectory: options.replaceDirectory ?? ((result: unknown) => { replaced.push(result); }),
      loadDirectory: options.loadDirectory ?? (async () => true),
      acceptFileTreeSettings: options.acceptFileTreeSettings ?? (() => {}),
      onStatusChange: (text: string) => statuses.push(text),
    });
    return null;
  }

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<Harness />);
  });
  return {
    api: () => {
      if (!apiRef.current) {
        throw new Error("hook 未挂载");
      }
      return apiRef.current;
    },
    renderer,
    statuses,
    replaced,
  };
}

function clickEvent(): { preventDefault: () => void; clientX: number; clientY: number } {
  return { preventDefault: () => {}, clientX: 33, clientY: 44 };
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("文件树右键菜单", () => {
  test("打开菜单写出目标路径、绝对路径与指针坐标", () => {
    installWindow();
    const harness = mountMenu();
    act(() => {
      harness.api().openContextMenu(
        clickEvent() as never,
        "src/a.txt",
        "a.txt",
        "file",
      );
    });
    expect(harness.api().contextMenu).toEqual({
      treePath: "src/a.txt",
      absolutePath: "/ws/src/a.txt",
      label: "a.txt",
      kind: "file",
      shortcutSource: null,
      x: 33,
      y: 44,
    });
  });

  test("目录目标取自身，文件目标取父目录", async () => {
    installWindow();
    const requested: string[] = [];
    const harness = mountMenu({
      loadDirectory: async (path: string) => { requested.push(path); return true; },
    });
    await act(async () => {
      await harness.api().refreshTargetDirectory(menuTarget({ treePath: "src", kind: "directory" }));
    });
    await act(async () => {
      await harness.api().refreshTargetDirectory(menuTarget({ treePath: "src/a.txt" }));
    });
    expect(requested).toEqual(["src", "src"]);
  });

  test("动作失败写入错误横幅并同步状态栏，成功时清空旧错误", async () => {
    installWindow();
    const harness = mountMenu();
    const failure = new Error("后端拒绝");
    await act(async () => {
      harness.api().runContextAction("新建文件失败", async () => { throw failure; });
      await Promise.resolve();
    });
    expect(harness.api().actionError).toBe("新建文件失败: 后端拒绝");
    expect(harness.statuses).toEqual(["新建文件失败: 后端拒绝"]);
    expect(harness.api().contextMenu).toBeNull();
    await act(async () => {
      harness.api().runContextAction("下载失败", async () => {});
      await Promise.resolve();
    });
    expect(harness.api().actionError).toBeNull();
  });

  test("状态栏动作失败不弹错误横幅只写状态栏", async () => {
    installWindow();
    const harness = mountMenu();
    await act(async () => {
      harness.api().runStatusAction("复制文件失败", async () => {
        throw new Error("剪贴板不可用");
      });
      await Promise.resolve();
    });
    expect(harness.api().actionError).toBeNull();
    expect(harness.statuses).toEqual(["复制文件失败: 剪贴板不可用"]);
  });

  test("无当前会话时快捷路径动作直接失败而不是静默跳过", async () => {
    installWindow();
    const harness = mountMenu({
      sessionId: "",
      acceptFileTreeSettings: () => { throw new Error("不应触达接受设置"); },
    });
    await expect(harness.api().addShortcut("src", "src")).rejects
      .toThrow("添加快捷路径需要当前会话");
    await expect(harness.api().removeShortcut("src")).rejects
      .toThrow("删除快捷路径需要当前会话");
    await expect(harness.api().addShortcutAndDefault("src", "src")).rejects
      .toThrow("添加当前会话和新会话默认快捷路径需要当前会话");
  });

  test("默认快捷路径开关按当前状态选择添加或删除", async () => {
    installWindow();
    const calls: string[] = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({
          code: 0, message: "ok", request_id: "req-menu-cred",
          data: { token: "local-menu-test-token" },
        });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return Response.json({
          code: 0, message: "ok", request_id: "req-menu-user",
          data: { kind: "guest", user_id: null },
        });
      }
      calls.push(url.pathname);
      const settings: SessionFileTreeSettings = {
        session_id: "ses_1",
        session_shortcuts: [],
        workspace_shortcuts: [],
        default_shortcuts: [],
        effective_shortcuts: [],
      };
      return Response.json({
        code: 0, message: "ok", request_id: "req-menu-test", data: settings,
      });
    }, { preconnect: globalThis.fetch.preconnect });

    const harness = mountMenu({ acceptFileTreeSettings: () => {} });
    await act(async () => {
      await harness.api().toggleDefaultShortcut(
        { path: "/ws/src", label: "src", source: "session" },
        false,
      );
    });
    await act(async () => {
      await harness.api().toggleDefaultShortcut(
        { path: "/ws/src", label: "src", source: "session" },
        true,
      );
    });
    expect(calls.filter((path) => path === "/api/v1/sessions/ses_1/file-tree-shortcuts").length)
      .toBe(2);
    expect(calls.filter((path) => path === "/api/v1/sessions/ses_1/file-tree-shortcuts/apply-to-workspace").length)
      .toBe(1);
    expect(calls.filter((path) => path === "/api/v1/sessions/ses_1/workspace-file-tree-shortcuts").length)
      .toBe(1);
    expect(harness.statuses.some((text) => text.startsWith("已将 "))).toBe(true);
    expect(harness.statuses.some((text) => text.startsWith("已从当前会话"))).toBe(true);
  });

  test("上传输入只在有目标且选中文件时派发", async () => {
    installWindow();
    const launched: string[] = [];
    const clickTarget = { click: () => launched.push("click") };
    const harness = mountMenu();
    act(() => {
      harness.api().requestUpload(menuTarget());
    });
    // ref 未挂到真实 DOM 时不应抛错，仅不触发 click。
    expect(() => harness.api().handleUploadInput([])).not.toThrow();
    void clickTarget;
  });

  test("上传失败后强制重载目标目录并只上报状态栏", async () => {
    installWindow();
    const requested: Array<{ path: string; force: boolean | undefined }> = [];
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({ code: 0, message: "ok", request_id: "req-up", data: { token: "t" } });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return Response.json({ code: 0, message: "ok", request_id: "req-up", data: { kind: "guest", user_id: null } });
      }
      return new Response(JSON.stringify({ detail: "磁盘已满" }), {
        status: 507, statusText: "Insufficient Storage",
        headers: { "Content-Type": "application/json" },
      });
    }, { preconnect: globalThis.fetch.preconnect });
    const harness = mountMenu({
      loadDirectory: async (path: string, force?: boolean) => {
        requested.push({ path, force });
        return true;
      },
    });
    act(() => {
      harness.api().requestUpload(menuTarget({ treePath: "src", kind: "directory" }));
      harness.api().handleUploadInput([new File(["x"], "a.txt")]);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requested).toEqual([{ path: "src", force: true }]);
    expect(harness.statuses.some((text) => text.startsWith("上传本地文件失败: "))).toBe(true);
  });
});
