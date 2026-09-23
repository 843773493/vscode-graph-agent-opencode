import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React, { type MutableRefObject } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api";
import * as gatewayApi from "../../gatewayApi";
import * as userAccessApi from "../../api/gateway/userAccess";
import * as userViewStateApi from "../../api/gateway/userViewState";
import * as workspaceSessionListRefresh from "./workspaceSessionListRefresh";
import type { AppState } from "../../types/frontend";
import { useWorkspaceBootstrap } from "./useWorkspaceBootstrap";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const API_PORT = 49_713;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
const originalImageDescriptor = Object.getOwnPropertyDescriptor(globalThis, "Image");

/** setIsBootstrapping 之外的延时都要压成 0，避免真实退避把测试拖慢。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void) => globalThis.setTimeout(handler, 0),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
      location: { port: String(API_PORT) },
    },
  });
}

function defaultUiSettings() {
  return {
    theme: { resolved_theme: { id: "warm" } },
    layout: {},
  };
}

function gatewayWorkspaceList(items: Array<{ workspace_id: string; name: string; status: string }>) {
  return {
    active_workspace_id: items[0]?.workspace_id ?? null,
    items: items.map((item) => ({
      ...item,
      root_path: `/tmp/${item.workspace_id}`,
      system_default: false,
    })),
  } as never;
}

function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    apiPort: API_PORT,
    gatewayWorkspaces: [
      { workspace_id: "ws-old", name: "旧工作区", status: "ready", root_path: "/tmp/ws-old" },
    ] as never,
    activeGatewayWorkspaceId: "ws-old",
    gatewayWorkspacesStale: false,
    sessionsByWorkspace: new Map(),
    sessions: [],
    gatewayUserAccess: null,
    gatewayUserViewStates: new Map(),
    turnTimelinesBySession: new Map(),
    unreadSessionKeys: new Set(),
    uiSettings: defaultUiSettings() as never,
    currentSession: null,
    currentSessionWorkspaceId: null,
    error: null,
    status: "",
    isBootstrapping: true,
    workspaceSwitching: false,
    ...overrides,
  } as unknown as AppState;
}

interface Mounted {
  refreshSessions: ReturnType<typeof useWorkspaceBootstrap>["refreshSessions"];
  state: () => AppState;
}

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];

async function mountBootstrap(initialState: AppState): Promise<Mounted> {
  let current = initialState;
  let hook: ReturnType<typeof useWorkspaceBootstrap> | undefined;

  function Probe(): React.ReactNode {
    hook = useWorkspaceBootstrap({
      apiPort: API_PORT,
      uiSettings: current.uiSettings,
      setState: (update) => {
        current = typeof update === "function"
          ? (update as (previous: AppState) => AppState)(current)
          : update;
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);
  // 挂载 effect 会排一个 0ms 的初始 bootstrap。下面的用例都要自己驱动 refreshSessions，
  // 必须在这里用 act + 0ms 定时器确定性地把它跑完：否则它会在用例的刷新途中才触发，
  // 按 refreshGeneration 作废掉用例的刷新，断言就会读到上一轮的 gatewayWorkspacesStale=true。
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
  return { refreshSessions: hook!.refreshSessions, state: () => current };
}

function spy<Name extends keyof typeof gatewayApi>(name: Name) {
  const s = spyOn(gatewayApi, name);
  restoreSpies.push(() => s.mockRestore());
  return s;
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restoreSpies.splice(0)) restore();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  for (const [name, descriptor] of [
    ["document", originalDocumentDescriptor],
    ["Image", originalImageDescriptor],
  ] as const) {
    if (descriptor) {
      Object.defineProperty(globalThis, name, descriptor);
    } else {
      Reflect.deleteProperty(globalThis, name);
    }
  }
});

/** 主题应用需要 document.documentElement；bun test 默认没有 DOM，这里补最小桩。 */
function installDocumentStub(): void {
  const style = { setProperty: () => {}, removeProperty: () => {} };
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: { documentElement: { style, dataset: {} }, querySelector: () => null },
  });
}

/** 背景图预加载用到的 Image 桩：src 一赋值立刻触发 error，模拟 404/断网。 */
function installFailingImageStub(): void {
  class FailingImage {
    onload: ((event: Event) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    set src(_value: string) {
      this.onerror?.(new Event("error"));
    }
  }
  Object.defineProperty(globalThis, "Image", {
    configurable: true,
    value: FailingImage,
  });
}

function stubCommonBootstrap(): void {
  const userAccess = spyOn(userAccessApi, "ensureGatewayUserAccess")
    .mockResolvedValue({ kind: "guest", user_id: null } as never);
  restoreSpies.push(() => userAccess.mockRestore());
  const userViewState = spyOn(userViewStateApi, "getLatestGatewayUserViewState")
    .mockResolvedValue(null as never);
  restoreSpies.push(() => userViewState.mockRestore());
  const getWorkspace = spyOn(api, "getWorkspace")
    .mockResolvedValue({ name: "工作区", root_path: "/tmp/ws" } as never);
  restoreSpies.push(() => getWorkspace.mockRestore());
  const listAgents = spyOn(api, "listAgents").mockResolvedValue([] as never);
  restoreSpies.push(() => listAgents.mockRestore());
}

describe("useWorkspaceBootstrap 失效标记", () => {
  test("Gateway 整体不可达时标记 gatewayWorkspacesStale 而不是静默沿用旧值", async () => {
    installWindow();
    stubCommonBootstrap();
    const uiSettings = spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    // 非 WorkspaceBootstrapUnavailableError 的错误：不是可重试错误，直接抛出。
    spy("listGatewayWorkspaces").mockRejectedValue(new Error("Gateway 不可达"));

    const mounted = await mountBootstrap(appState({ gatewayWorkspacesStale: false }));
    await act(async () => {
      await expect(mounted.refreshSessions()).rejects.toThrow("Gateway 不可达");
    });

    const next = mounted.state();
    // 旧结构被保留用于过渡展示，但必须显式标记失效，UI 才能判定它不可信。
    expect(next.gatewayWorkspaces).toHaveLength(1);
    expect(next.gatewayWorkspacesStale).toBe(true);
    expect(next.status).toBe("初始化失败");
    expect(uiSettings).toHaveBeenCalled();
  });

  test("部分工作区会话列表读取失败时标记失效，全部成功时清除标记", async () => {
    installWindow();
    stubCommonBootstrap();
    spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    spy("listGatewayWorkspaces").mockResolvedValue(
      gatewayWorkspaceList([{ workspace_id: "ws-1", name: "工作区一", status: "ready" }]),
    );

    // 先失败：会话列表快照读取被拒绝。
    const failing = spyOn(workspaceSessionListRefresh, "fetchWorkspaceSessionListSnapshot")
      .mockRejectedValue(new Error("会话目录不可读"));
    restoreSpies.push(() => failing.mockRestore());
    const failedRun = await mountBootstrap(appState());
    await act(async () => {
      // 复用当前 UI 设置，跳过主题加载（该步骤需要真实 DOM）。
      await failedRun.refreshSessions(undefined, { reuseCurrentUiSettings: true });
    });
    expect(failedRun.state().gatewayWorkspacesStale).toBe(true);

    // 再成功：同一 hook 实例重新刷新后必须清除失效标记。
    failing.mockResolvedValue({
      apiPort: API_PORT,
      workspaceId: "ws-1",
      generation: 0,
      sessions: [],
    } as never);
    const isCurrent = spyOn(workspaceSessionListRefresh, "isCurrentWorkspaceSessionListSnapshot")
      .mockReturnValue(true);
    restoreSpies.push(() => isCurrent.mockRestore());
    await act(async () => {
      await failedRun.refreshSessions(undefined, { reuseCurrentUiSettings: true });
    });
    expect(failedRun.state().gatewayWorkspacesStale).toBe(false);
  });

  test("背景图 404 时工作区初始化仍成功，只留下可见的背景图警告", async () => {
    installWindow();
    installDocumentStub();
    installFailingImageStub();
    stubCommonBootstrap();
    spy("getGatewayUiSettings").mockResolvedValue({
      theme: {
        resolved_theme: {
          id: "warm",
          color_scheme: "light",
          tokens: { "--bt-page-background": "#f2ecd9" },
          background_image_url: "/api/gateway/ui-assets/background-404",
        },
      },
      layout: {},
    } as never);
    spy("listGatewayWorkspaces").mockResolvedValue(
      gatewayWorkspaceList([{ workspace_id: "ws-1", name: "工作区一", status: "ready" }]),
    );
    const snapshot = spyOn(workspaceSessionListRefresh, "fetchWorkspaceSessionListSnapshot")
      .mockResolvedValue({
        apiPort: API_PORT,
        workspaceId: "ws-1",
        generation: 0,
        sessions: [],
      } as never);
    restoreSpies.push(() => snapshot.mockRestore());

    // 关键验收：不传 reuseCurrentUiSettings，真正走一遍主题加载链路。
    const mounted = await mountBootstrap(appState());
    await act(async () => {
      await mounted.refreshSessions();
    });

    const next = mounted.state();
    // 工作区初始化必须成功：没有把背景图失败升级成初始化失败。
    expect(next.error).toBeNull();
    expect(next.isBootstrapping).toBe(false);
    expect(next.status).not.toBe("初始化失败");
    expect(next.workspaceName).toBe("工作区");
    // 且失败必须可见，不得静默。
    expect(next.themeBackgroundWarning).toContain(
      "背景图片加载失败: /api/gateway/ui-assets/background-404",
    );
  });
});
