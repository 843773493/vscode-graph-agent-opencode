import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WebUiSettings, WebUiSettingsUpdate } from "../../types/backend";
import type { ExtensionWindowRequest } from "../../utils/extensionResourceWindow";
import { useWorkbenchLayoutPreferences } from "./useWorkbenchLayoutPreferences";

/**
 * 工作台布局偏好链路的契约：扩展窗口里本地初始态独立于主窗口设置；底部面板状态
 * 按工作区保存在本地镜像，同时把高度/标签落盘；用户可见操作失败必须写进状态。
 */

const mountedRenderers: ReactTestRenderer[] = [];

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
});

function settings(layout: WebUiSettings["layout"] = {}): WebUiSettings {
  return {
    layout,
    session_sidebar: {},
    workspace_file_tree: { expanded_paths_by_workspace: {} },
    gateway_console: { view: "routing" },
    recent_local_workspace_paths: [],
  } as unknown as WebUiSettings;
}

interface MountOptions {
  uiSettings?: WebUiSettings;
  extensionWindowRequest?: ExtensionWindowRequest | null;
  bottomPanelWorkspaceId?: string | null;
  updateUiSettings?: (input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate)) => Promise<void>;
}

function mountHook(options: MountOptions = {}) {
  const statuses: string[] = [];
  const persisted: Array<WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate)> = [];
  // 设置对象必须保持稳定身份：同步 effect 以它为依赖，逐次新建会触发无限重渲染。
  const uiSettings = options.uiSettings ?? settings();
  let hook: ReturnType<typeof useWorkbenchLayoutPreferences> | undefined;

  function Probe(): React.ReactNode {
    hook = useWorkbenchLayoutPreferences({
      uiSettings,
      extensionWindowRequest: options.extensionWindowRequest ?? null,
      bottomPanelWorkspaceId: "bottomPanelWorkspaceId" in options
        ? options.bottomPanelWorkspaceId!
        : "ws-1",
      updateUiSettings: options.updateUiSettings ?? (async (input) => {
        persisted.push(input);
      }),
      setStatus: (text) => statuses.push(text),
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  return {
    mount: async () => {
      await act(async () => {
        renderer = create(<Probe />);
      });
      mountedRenderers.push(renderer!);
    },
    hook: () => hook!,
    statuses,
    persisted,
  };
}

describe("工作台布局偏好链路", () => {
  test("主窗口从后端设置恢复初始展示态", async () => {
    const mounted = mountHook({
      uiSettings: settings({
        workbench_view: "gateway",
        auxiliary_visible: false,
        chat_visible: false,
        auxiliary_tab: "changes",
        auxiliary_tab_order: ["changes", "files", "debug", "resources"],
      }),
    });
    await mounted.mount();

    const hook = mounted.hook();
    expect(hook.workbenchView).toBe("gateway");
    expect(hook.auxiliaryVisible).toBe(false);
    expect(hook.chatVisible).toBe(false);
    expect(hook.auxiliaryTab).toBe("changes");
    expect(hook.auxiliaryTabOrder).toEqual([
      "changes",
      "files",
      "debug",
      "resources",
    ]);
  });

  test("扩展窗口的本地初始态不继承主窗口设置", async () => {
    const mounted = mountHook({
      extensionWindowRequest: {
        kind: "debug",
        resourceId: "r1",
        workspaceId: "ws-1",
        sessionId: "ses-1",
      },
      uiSettings: settings({ auxiliary_visible: false, chat_visible: true }),
    });
    await mounted.mount();

    const hook = mounted.hook();
    expect(hook.auxiliaryTab).toBe("debug");
    expect(hook.auxiliaryVisible).toBe(true);
    expect(hook.chatVisible).toBe(false);
    // 扩展窗口里底部面板强制不可见，避免遮挡资源区。
    expect(hook.panelVisible).toBe(false);
  });

  test("切换工作台视图同时更新展示态并落库", async () => {
    const mounted = mountHook();
    await mounted.mount();

    act(() => mounted.hook().handleWorkbenchViewChange("gateway"));

    expect(mounted.hook().workbenchView).toBe("gateway");
    expect(mounted.persisted).toEqual([{ layout: { workbench_view: "gateway" } }]);
  });

  test("底部面板变更写本地镜像并按工作区落盘", async () => {
    const mounted = mountHook();
    await mounted.mount();

    await act(async () => {
      mounted.hook().updateBottomPanelState({ tab: "terminal", terminalId: "t1" });
    });

    expect(mounted.hook().bottomPanelState.tab).toBe("terminal");
    expect(mounted.hook().bottomPanelState.terminalId).toBe("t1");
    expect(mounted.persisted).toHaveLength(1);
  });

  test("没有归属工作区时底部面板变更不落盘", async () => {
    const mounted = mountHook({ bottomPanelWorkspaceId: null });
    await mounted.mount();

    await act(async () => {
      mounted.hook().updateBottomPanelState({ visible: true });
    });

    expect(mounted.persisted).toEqual([]);
    expect(mounted.hook().bottomPanelState.visible).toBe(false);
  });

  test("落盘失败必须写进状态而不是静默吞掉", async () => {
    const mounted = mountHook({
      updateUiSettings: async () => {
        throw new Error("后端拒绝了这次设置写入");
      },
    });
    await mounted.mount();

    await act(async () => {
      mounted.hook().persistLayoutSettings({ auxiliary_visible: true });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mounted.statuses).toEqual([
      "保存页面设置失败: 后端拒绝了这次设置写入",
    ]);
  });
});
