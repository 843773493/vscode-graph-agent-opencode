import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api";
import type { WebUiLayoutSettings, WorkspaceFileContent } from "../../types/backend";
import WarmConfirmProvider from "../../components/shell/WarmConfirmProvider";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import { useWorkspacePreviewTabs } from "./useWorkspacePreviewTabs";

const API_PORT = 49_741;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

/** hook 会使用 window.setTimeout 做布局落库防抖与 beforeunload 监听，必须装上桩。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(API_PORT) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function fileContent(path: string): WorkspaceFileContent {
  return {
    path,
    language: "text",
    content: `内容 ${path}`,
    revision: "rev-1",
  } as unknown as WorkspaceFileContent;
}

function layout(): WebUiLayoutSettings {
  return {
    workspace_preview_visible: true,
    workspace_preview_maximized: false,
    workspace_preview_file_paths: ["/a.txt", "/b.txt", "/c.txt"],
    workspace_preview_active_file_path: "/a.txt",
  } as unknown as WebUiLayoutSettings;
}

const renderers: ReactTestRenderer[] = [];
const restores: Array<() => void> = [];

afterEach(() => {
  for (const renderer of renderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restores.splice(0)) restore();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function mountPreviewTabs(): Promise<{
  current: () => ReturnType<typeof useWorkspacePreviewTabs>;
}> {
  return await mountPreviewTabsWith(() => undefined);
}

async function mountPreviewTabsWith(
  onPersistLayout: (layout: WebUiLayoutSettings) => void,
): Promise<{
  current: () => ReturnType<typeof useWorkspacePreviewTabs>;
}> {
  installWindow();
  let current: ReturnType<typeof useWorkspacePreviewTabs> | undefined;

  function Probe(): React.ReactNode {
    current = useWorkspacePreviewTabs({
      apiPort: API_PORT,
      workspaceId: "workspace-preview",
      workspaceRoot: "/workspace-root",
      settingsLoaded: true,
      restoredLayout: layout(),
      onPersistLayout,
      onStatusChange: () => undefined,
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(
      <WarmConfirmProvider>
        <Probe />
      </WarmConfirmProvider>,
    );
  });
  renderers.push(renderer!);
  return { current: () => current! };
}

describe("useWorkspacePreviewTabs 预览页签切换的在途归属", () => {
  /** 挂载后依次点击两个占位页签，制造「先点的响应后到」这一真实序列。 */
  async function selectTwoTabs() {
    const requestB = deferred<WorkspaceFileContent>();
    const requestC = deferred<WorkspaceFileContent>();
    const getContent = spyOn(api, "getWorkspaceFileContent")
      .mockImplementation(async (_port, path) => {
        if (path === "/b.txt") return await requestB.promise;
        if (path === "/c.txt") return await requestC.promise;
        return fileContent(path);
      });
    restores.push(() => getContent.mockRestore());

    const mounted = await mountPreviewTabs();
    await flush();
    act(() => {
      mounted.current().selectWorkspacePreviewTab("/b.txt");
    });
    await flush();
    act(() => {
      mounted.current().selectWorkspacePreviewTab("/c.txt");
    });
    await flush();
    return { ...mounted, requestB, requestC };
  }

  test("先点 B 再点 C 时，B 的迟到响应不得把活动页签抢回 B", async () => {
    const { current, requestB, requestC } = await selectTwoTabs();
    expect(current().activePath).toBe("/c.txt");

    // 用户已经切到 C，B 的响应此刻才回来。
    requestB.resolve(fileContent("/b.txt"));
    await flush();
    expect(current().activePath).toBe("/c.txt");
    expect(current().loadingPath).toBe("/c.txt");

    requestC.resolve(fileContent("/c.txt"));
    await flush();
    expect(current().activePath).toBe("/c.txt");
    expect(current().loadingPath).toBeNull();
  });

  test("先点 B 再点 C 时，B 的迟到失败不得污染 C 的加载态与错误通道", async () => {
    const { current, requestB, requestC } = await selectTwoTabs();

    requestB.reject(new Error("文件读取失败 500"));
    await flush();
    // C 仍在装载中：B 的失败既不能写成全局错误，也不能把 C 的加载指示清掉。
    expect(current().error).toBeNull();
    expect(current().loadingPath).toBe("/c.txt");
    expect(current().activePath).toBe("/c.txt");

    requestC.resolve(fileContent("/c.txt"));
    await flush();
    expect(current().error).toBeNull();
    expect(current().loadingPath).toBeNull();
    expect(current().activePath).toBe("/c.txt");
  });

  test("页签在装载中被关闭后，迟到响应不得把它重新加回列表", async () => {
    const requestB = deferred<WorkspaceFileContent>();
    const getContent = spyOn(api, "getWorkspaceFileContent")
      .mockImplementation(async (_port, path) => (
        path === "/b.txt" ? await requestB.promise : fileContent(path)
      ));
    restores.push(() => getContent.mockRestore());

    const { current } = await mountPreviewTabs();
    await flush();
    act(() => {
      current().selectWorkspacePreviewTab("/b.txt");
    });
    await flush();
    // 用户不等 B 读完就关掉它。
    await act(async () => {
      await current().closeWorkspaceFilePreview("/b.txt");
    });
    expect(current().tabs.some((tab) => tab.path === "/b.txt")).toBe(false);

    requestB.resolve(fileContent("/b.txt"));
    await flush();
    expect(current().tabs.some((tab) => tab.path === "/b.txt")).toBe(false);
  });

  test("恢复读取被用户点击顶替后，布局落库闸门不得被永久关闭", async () => {
    const persisted: WebUiLayoutSettings[] = [];
    const requestA = deferred<WorkspaceFileContent>();
    const getContent = spyOn(api, "getWorkspaceFileContent")
      .mockImplementation(async (_port, path) => (
        path === "/a.txt" ? await requestA.promise : fileContent(path)
      ));
    restores.push(() => getContent.mockRestore());

    const { current } = await mountPreviewTabsWith((layout) => persisted.push(layout));
    await flush();
    // 挂载恢复的 /a.txt 尚未返回时，用户先点了 B 并等它装载完成。
    act(() => {
      current().selectWorkspacePreviewTab("/b.txt");
    });
    await flush();
    // 顶替发生后，恢复读取才迟到返回：加载指示属于 B，但落库闸门必须已放行。
    requestA.resolve(fileContent("/a.txt"));
    await flush();

    act(() => {
      current().setVisible(true);
    });
    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 260));
    });
    expect(persisted.length).toBeGreaterThan(0);
    expect(current().activePath).toBe("/b.txt");
  });
});
