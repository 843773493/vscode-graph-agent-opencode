import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WorkspaceAuxiliaryTab } from "../components/workspace/WorkspaceAuxiliaryPanel";
import type { WorkspaceBottomPanelState } from "../state/workspaceBottomPanel";
import type { WebUiSettingsUpdate } from "../types/backend";
import { useWorkbenchPanelRouting } from "./useWorkbenchPanelRouting";

interface MountOptions {
  auxiliaryVisible?: boolean;
  chatVisible?: boolean;
  bottomPanelState?: WorkspaceBottomPanelState;
  bottomPanelWorkspaceId?: string | null;
  extensionWindowRequested?: boolean;
}

interface MountedHook {
  hook: ReturnType<typeof useWorkbenchPanelRouting>;
  layoutWrites: Array<WebUiSettingsUpdate["layout"]>;
  statuses: string[];
  panelUpdates: Array<Partial<WorkspaceBottomPanelState>>;
  fallbackWrites: boolean[];
  auxiliaryTab: () => WorkspaceAuxiliaryTab | null;
  auxiliaryTabOrder: () => WorkspaceAuxiliaryTab[] | null;
  auxiliaryVisible: () => boolean;
  chatVisible: () => boolean;
}

const mountedRenderers: ReactTestRenderer[] = [];

/** 用 react-test-renderer 的 Probe 组件挂载 hook，与 useBottomPanelResize.test.tsx 一致。 */
async function mountHook(options: MountOptions = {}): Promise<MountedHook> {
  const layoutWrites: Array<WebUiSettingsUpdate["layout"]> = [];
  const statuses: string[] = [];
  const panelUpdates: Array<Partial<WorkspaceBottomPanelState>> = [];
  const fallbackWrites: boolean[] = [];
  let currentAuxiliaryTab: WorkspaceAuxiliaryTab | null = null;
  let currentAuxiliaryTabOrder: WorkspaceAuxiliaryTab[] | null = null;
  let currentAuxiliaryVisible = options.auxiliaryVisible ?? true;
  let currentChatVisible = options.chatVisible ?? true;
  let hook: ReturnType<typeof useWorkbenchPanelRouting> | undefined;

  const bottomPanelState: WorkspaceBottomPanelState = options.bottomPanelState ?? {
    visible: false,
    height: 300,
    tab: "output",
    terminalId: null,
  };

  function Probe(): React.ReactNode {
    hook = useWorkbenchPanelRouting({
      auxiliaryVisible: currentAuxiliaryVisible,
      setAuxiliaryVisible: (update) => {
        currentAuxiliaryVisible = typeof update === "function"
          ? update(currentAuxiliaryVisible)
          : update;
      },
      chatVisible: currentChatVisible,
      setChatVisible: (update) => {
        currentChatVisible = typeof update === "function" ? update(currentChatVisible) : update;
      },
      setAuxiliaryTab: (update) => {
        currentAuxiliaryTab = typeof update === "function" && currentAuxiliaryTab
          ? update(currentAuxiliaryTab)
          : update as WorkspaceAuxiliaryTab;
      },
      setAuxiliaryTabOrder: (update) => {
        currentAuxiliaryTabOrder = typeof update === "function" && currentAuxiliaryTabOrder
          ? update(currentAuxiliaryTabOrder)
          : update as WorkspaceAuxiliaryTab[];
      },
      bottomPanelState,
      updateBottomPanelState: (patch) => {
        panelUpdates.push(patch);
      },
      bottomPanelWorkspaceId: "bottomPanelWorkspaceId" in options
        ? options.bottomPanelWorkspaceId!
        : "ws-panel",
      extensionWindowRequested: options.extensionWindowRequested ?? false,
      setExtensionWindowFallback: (update) => {
        fallbackWrites.push(typeof update === "function" ? update(false) : update);
      },
      persistLayoutSettings: (layout) => {
        layoutWrites.push(layout);
      },
      setStatus: (text) => {
        statuses.push(text);
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);

  return {
    hook: hook!,
    layoutWrites,
    statuses,
    panelUpdates,
    fallbackWrites,
    auxiliaryTab: () => currentAuxiliaryTab,
    auxiliaryTabOrder: () => currentAuxiliaryTabOrder,
    auxiliaryVisible: () => currentAuxiliaryVisible,
    chatVisible: () => currentChatVisible,
  };
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
});

describe("useWorkbenchPanelRouting 面板路由契约", () => {
  test("handleToggleAuxiliaryPanel 取反显隐并持久化 auxiliary_visible", async () => {
    const mounted = await mountHook({ auxiliaryVisible: true });

    act(() => mounted.hook.handleToggleAuxiliaryPanel());

    expect(mounted.auxiliaryVisible()).toBe(false);
    expect(mounted.layoutWrites).toEqual([{ auxiliary_visible: false }]);
    expect(mounted.statuses).toEqual(["右侧侧边栏已切换为收起"]);
  });

  test("handleToggleChatPanel 取反显隐并持久化 chat_visible", async () => {
    const mounted = await mountHook({ chatVisible: false });

    act(() => mounted.hook.handleToggleChatPanel());

    expect(mounted.chatVisible()).toBe(true);
    expect(mounted.layoutWrites).toEqual([{ chat_visible: true }]);
    expect(mounted.statuses).toEqual(["会话区已展开"]);
  });

  test("handleAuxiliaryTabChange 在非扩展窗口时写入 auxiliary_tab", async () => {
    const mounted = await mountHook({ extensionWindowRequested: false });

    act(() => mounted.hook.handleAuxiliaryTabChange("changes"));

    expect(mounted.auxiliaryTab()).toBe("changes");
    expect(mounted.layoutWrites).toEqual([{ auxiliary_tab: "changes" }]);
  });

  test("handleAuxiliaryTabChange 在扩展窗口模式下切换标签但不持久化", async () => {
    const mounted = await mountHook({ extensionWindowRequested: true });

    act(() => mounted.hook.handleAuxiliaryTabChange("changes"));

    expect(mounted.auxiliaryTab()).toBe("changes");
    expect(mounted.layoutWrites).toEqual([]);
  });

  test("handleAuxiliaryTabReorder 在非扩展窗口时写入 auxiliary_tab_order", async () => {
    const mounted = await mountHook({ extensionWindowRequested: false });
    const nextOrder: WorkspaceAuxiliaryTab[] = ["changes", "files", "debug", "resources"];

    act(() => mounted.hook.handleAuxiliaryTabReorder(nextOrder));

    expect(mounted.auxiliaryTabOrder()).toEqual(nextOrder);
    expect(mounted.layoutWrites).toEqual([{ auxiliary_tab_order: nextOrder }]);
  });

  test("handleAuxiliaryTabReorder 在扩展窗口模式下重排但不持久化", async () => {
    const mounted = await mountHook({ extensionWindowRequested: true });

    act(() => mounted.hook.handleAuxiliaryTabReorder(["debug", "resources"]));

    expect(mounted.auxiliaryTabOrder()).toEqual(["debug", "resources"]);
    expect(mounted.layoutWrites).toEqual([]);
  });

  test("openAuxiliaryTab(\"resources\") 展开并切标签，但不清扩展窗口回退", async () => {
    const mounted = await mountHook({ auxiliaryVisible: false });

    act(() => mounted.hook.openAuxiliaryTab("resources"));

    expect(mounted.auxiliaryVisible()).toBe(true);
    expect(mounted.auxiliaryTab()).toBe("resources");
    expect(mounted.layoutWrites).toEqual([
      { auxiliary_visible: true, auxiliary_tab: "resources" },
    ]);
    expect(mounted.fallbackWrites).toEqual([]);
  });

  test("openAuxiliaryTab(\"files\") 清除扩展窗口回退", async () => {
    const mounted = await mountHook({ auxiliaryVisible: false });

    act(() => mounted.hook.openAuxiliaryTab("files"));

    expect(mounted.auxiliaryTab()).toBe("files");
    expect(mounted.layoutWrites).toEqual([
      { auxiliary_visible: true, auxiliary_tab: "files" },
    ]);
    expect(mounted.fallbackWrites).toEqual([false]);
  });

  test("openAuxiliaryTab 在扩展窗口模式下不持久化布局", async () => {
    const mounted = await mountHook({ extensionWindowRequested: true });

    act(() => mounted.hook.openAuxiliaryTab("debug"));

    expect(mounted.auxiliaryTab()).toBe("debug");
    expect(mounted.layoutWrites).toEqual([]);
    expect(mounted.fallbackWrites).toEqual([false]);
  });

  test("handleTogglePanel 通过 updateBottomPanelState 取反显隐，不持久化布局", async () => {
    const mounted = await mountHook({
      bottomPanelState: { visible: false, height: 300, tab: "output", terminalId: null },
    });

    act(() => mounted.hook.handleTogglePanel());

    expect(mounted.panelUpdates).toEqual([{ visible: true }]);
    expect(mounted.layoutWrites).toEqual([]);
    expect(mounted.statuses).toEqual(["底部面板已展开"]);
  });

  test("handleTogglePanel 在可见时收起底部面板", async () => {
    const mounted = await mountHook({
      bottomPanelState: { visible: true, height: 300, tab: "output", terminalId: null },
    });

    act(() => mounted.hook.handleTogglePanel());

    expect(mounted.panelUpdates).toEqual([{ visible: false }]);
    expect(mounted.statuses).toEqual(["底部面板已收起"]);
  });

  test("openTerminalPanel 无活动工作区时报错早退，不写面板状态", async () => {
    const mounted = await mountHook({ bottomPanelWorkspaceId: null });

    act(() => mounted.hook.openTerminalPanel("terminal-1"));

    expect(mounted.statuses).toEqual(["打开终端失败：当前没有活动工作区"]);
    expect(mounted.panelUpdates).toEqual([]);
  });

  test("openTerminalPanel 有活动工作区时在底部面板打开终端", async () => {
    const mounted = await mountHook({ bottomPanelWorkspaceId: "ws-panel" });

    act(() => mounted.hook.openTerminalPanel("terminal-7"));

    expect(mounted.panelUpdates).toEqual([
      { visible: true, tab: "terminal", terminalId: "terminal-7" },
    ]);
    expect(mounted.statuses).toEqual(["已在主窗口底部面板打开终端：terminal-7"]);
  });
});
