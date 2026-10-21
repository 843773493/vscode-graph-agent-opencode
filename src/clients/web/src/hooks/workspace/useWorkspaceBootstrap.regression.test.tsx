import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React, { type MutableRefObject } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api";
import { HttpRequestError } from "../../api/http";
import * as gatewayApi from "../../gatewayApi";
import * as userAccessApi from "../../api/gateway/userAccess";
import * as userViewStateApi from "../../api/gateway/userViewState";
import * as workspaceSessionListRefresh from "./workspaceSessionListRefresh";
import type { AppState } from "../../types/frontend";
import type { GatewayWorkspaceList, Session } from "../../types/backend";
import { useWorkspaceBootstrap } from "./useWorkspaceBootstrap";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import { createSerialTaskQueue } from "../runtime/serialTaskQueue";
import { useGatewayWorkspaceHierarchy } from "../gatewayWorkspace/useGatewayWorkspaceHierarchy";
import { useGatewayWorkspaceActivation } from "../gatewayWorkspace/useGatewayWorkspaceActivation";
import { useWorkspaceRefreshOrchestration } from "./useWorkspaceRefreshOrchestration";

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

function gatewayWorkspaceList(items: Array<{ workspace_id: string; name: string; status: string }>): GatewayWorkspaceList {
  return {
    active_workspace_id: items[0]?.workspace_id ?? null,
    items: items.map((item, index) => ({
      ...item,
      backend_workspace_id: `00000000-0000-0000-0000-${String(index + 1).padStart(12, "0")}`,
      connection_kind: "local",
      system_default: false,
    })),
  } as GatewayWorkspaceList;
}

function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    apiPort: API_PORT,
    gatewayWorkspaces: [
      { workspace_id: "ws-old", name: "旧工作区", status: "ready", },
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
  refreshStatuses: ReturnType<typeof useWorkspaceBootstrap>["refreshGatewayWorkspaceStatuses"];
  setParent: ReturnType<typeof useGatewayWorkspaceHierarchy>;
  selectionIntentRef: MutableRefObject<number>;
  setLatest: (state: AppState) => void;
  state: () => AppState;
}

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];

async function mountBootstrap(
  initialState: AppState, flushStartup = true,
  initialSelection: { workspaceId: string; sessionId: string } | null = null,
): Promise<Mounted> {
  let current = initialState;
  let hook: ReturnType<typeof useWorkspaceBootstrap> | undefined;
  const selectionIntentRef = { current: 0 };
  const gatewayListRequestRef = { current: 0 };
  const queue = createSerialTaskQueue();
  let setParent!: ReturnType<typeof useGatewayWorkspaceHierarchy>;
  const setState = (update: Parameters<import("../contentViewLoaderTypes").SetAppState>[0]) => {
    current = typeof update === "function" ? update(current) : update;
  };

  function Probe(): React.ReactNode {
    hook = useWorkspaceBootstrap({
      initialSelection,
      apiPort: API_PORT,
      selectionIntentRef,
      gatewayListRequestRef,
      runGatewayWorkspaceListTask: queue.enqueue,
      uiSettings: current.uiSettings,
      readState: () => current,
      setState,
    });
    setParent = useGatewayWorkspaceHierarchy(API_PORT, setState, gatewayListRequestRef, queue.enqueue, selectionIntentRef);
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
  if (flushStartup) await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
  return { refreshSessions: hook!.refreshSessions, refreshStatuses: hook!.refreshGatewayWorkspaceStatuses,
    setParent, selectionIntentRef, setLatest: (next) => { current = next; }, state: () => current };
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
    .mockResolvedValue({ workspace_id: "00000000-0000-0000-0000-000000000001", name: "工作区" });
  restoreSpies.push(() => getWorkspace.mockRestore());
  const listAgents = spyOn(api, "listAgents").mockResolvedValue([] as never);
  restoreSpies.push(() => listAgents.mockRestore());
}

describe("useWorkspaceBootstrap 失效标记", () => {
  for (const persisted of [false, true]) {
    test(`${persisted ? "重载时定点恢复" : "普通刷新保留"}首页外的已选会话，不拉取全部分页`, async () => {
      installWindow(); stubCommonBootstrap(); installDocumentStub();
      const selected: Session = {
        session_id: `ses_bootstrap_off_page_${persisted}`, workspace_id: "00000000-0000-0000-0000-000000000001",
        resource_identity: "fixture-off-page", uri: "boxteam://fixture/off-page", scope: "workspace",
        title: "首页外会话", title_source: "user", current_agent_id: "default", parent_session_id: null,
        created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z",
      };
      const first = { ...selected, session_id: "ses_bootstrap_first", title: "首页会话" };
      spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
      spy("listGatewayWorkspaces").mockResolvedValue(gatewayWorkspaceList([
        { workspace_id: "ws-1", name: "工作区一", status: "ready" },
      ]));
      const list = spyOn(api, "listSessions").mockResolvedValue({ items: [first], next_cursor: "next", has_more: true });
      const detail = spyOn(api, "getSession").mockResolvedValue(selected);
      restoreSpies.push(() => list.mockRestore(), () => detail.mockRestore());
      if (persisted) {
        const user = spyOn(userAccessApi, "ensureGatewayUserAccess").mockResolvedValue({ kind: "user", user_id: "user-page" } as never);
        const view = spyOn(userViewStateApi, "getLatestGatewayUserViewState").mockResolvedValue({
          user_id: "user-page", workspace_id: "ws-1", session_id: selected.session_id,
        } as never);
        restoreSpies.push(() => user.mockRestore(), () => view.mockRestore());
      }
      const mounted = await mountBootstrap(appState({
        activeGatewayWorkspaceId: "ws-1", currentSessionWorkspaceId: persisted ? null : "ws-1",
        currentSession: persisted ? null : selected,
        sessions: persisted ? [] : [selected],
        sessionsByWorkspace: new Map([["ws-1", persisted ? [] : [selected]]]),
        gatewayUserAccess: { kind: "guest", user_id: null } as never,
      }));
      expect(mounted.state().currentSession).toEqual(selected);
      expect(mounted.state().sessions.map((session) => session.session_id)).toEqual([first.session_id, selected.session_id]);
      expect(list).toHaveBeenCalledTimes(1);
      expect(detail).toHaveBeenCalledTimes(persisted ? 1 : 0);
      if (persisted) expect(detail).toHaveBeenCalledWith(API_PORT, selected.session_id, "ws-1", { signal: expect.any(AbortSignal) });
    });
  }

  test("用户切换时不定点读取旧用户偏好的首页外会话，只恢复新用户的选择", async () => {
    installWindow(); stubCommonBootstrap(); installDocumentStub();
    const first = { session_id: "ses_new_first", title: "新用户首页会话" } as Session;
    const restored = { session_id: "ses_new_persisted", title: "新用户首页外会话" } as Session;
    const old = { session_id: "ses_old_preferred", title: "旧用户会话" } as Session;
    spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    spy("listGatewayWorkspaces").mockResolvedValue(gatewayWorkspaceList([
      { workspace_id: "ws-1", name: "工作区一", status: "ready" },
    ]));
    const user = spyOn(userAccessApi, "ensureGatewayUserAccess")
      .mockResolvedValue({ kind: "user", user_id: "user-new" } as never);
    const view = spyOn(userViewStateApi, "getLatestGatewayUserViewState").mockResolvedValue({
      user_id: "user-new", workspace_id: "ws-1", session_id: restored.session_id,
    } as never);
    const list = spyOn(api, "listSessions").mockResolvedValue({ items: [first], next_cursor: "next", has_more: true });
    const detail = spyOn(api, "getSession").mockImplementation(async (_port, id) => {
      expect(id).toBe(restored.session_id);
      return restored;
    });
    restoreSpies.push(() => user.mockRestore(), () => view.mockRestore(), () => list.mockRestore(), () => detail.mockRestore());
    const mounted = await mountBootstrap(appState({
      activeGatewayWorkspaceId: "ws-1", currentSessionWorkspaceId: "ws-1", currentSession: old,
      sessions: [old], sessionsByWorkspace: new Map([["ws-1", [old]]]),
      gatewayUserAccess: { kind: "user", user_id: "user-old" } as never,
    }), false);
    // 显式刷新取代尚未触发的初始恢复，避免两轮初始化争用同一断言计数。
    mounted.selectionIntentRef.current += 1;
    await act(async () => { await mounted.refreshSessions(old.session_id); });
    expect(detail).toHaveBeenCalledTimes(1);
    expect(detail).toHaveBeenCalledWith(API_PORT, restored.session_id, "ws-1", { signal: expect.any(AbortSignal) });
    expect(list).toHaveBeenCalledTimes(1);
    expect(mounted.state().currentSession).toEqual(restored);
    expect(mounted.state().sessions).toEqual([first, restored]);
    expect(mounted.state().sessionsByWorkspace.get("ws-1")).toEqual([first, restored]);
  });

  test("真实 React 激活组合在成功后立即读取状态时保留已排入 bootstrap 的选择与会话镜像", async () => {
    installWindow(); stubCommonBootstrap(); installDocumentStub();
    let list = gatewayWorkspaceList([
      { workspace_id: "ws-old", name: "旧工作区", status: "ready" },
      { workspace_id: "ws-1", name: "目标工作区", status: "ready" },
    ]);
    spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    const lists = spy("listGatewayWorkspaces").mockImplementation(async () => list);
    const activation = spy("activateGatewayWorkspace").mockImplementation(async (_port, workspaceId) => {
      list = { ...list, active_workspace_id: workspaceId };
      return workspaceId;
    });
    const snapshots = spyOn(workspaceSessionListRefresh, "fetchWorkspaceSessionListSnapshot")
      .mockImplementation(async (_port, target) => ({ apiPort: API_PORT, workspaceId: target.workspace_id,
        generation: 1, sessions: [], complete: true }));
    const isCurrent = spyOn(workspaceSessionListRefresh, "isCurrentWorkspaceSessionListSnapshot").mockReturnValue(true);
    restoreSpies.push(() => snapshots.mockRestore(), () => isCurrent.mockRestore());
    let current = appState();
    let activate!: ReturnType<typeof useGatewayWorkspaceActivation>["activateGatewayWorkspace"];
    function Harness(): React.ReactNode {
      const [state, setState] = React.useState(current);
      current = state;
      const latestStateRef = React.useRef(state); latestStateRef.current = state;
      const selectionIntentRef = React.useRef(0);
      const gatewayListRequestRef = React.useRef(0);
      const queue = React.useRef(createSerialTaskQueue()).current;
      const bootstrap = useWorkspaceBootstrap({ apiPort: API_PORT, uiSettings: state.uiSettings,
        readState: () => latestStateRef.current, setState, selectionIntentRef, gatewayListRequestRef, runGatewayWorkspaceListTask: queue.enqueue });
      const refresh = useWorkspaceRefreshOrchestration({ apiPort: API_PORT, gatewayWorkspaces: state.gatewayWorkspaces,
        setState, refreshSessions: bootstrap.refreshSessions, abortCurrentStream: () => {}, selectionIntentRef });
      activate = useGatewayWorkspaceActivation({ apiPort: API_PORT, currentSessionId: null, latestStateRef,
        setState, invalidateWorkspaceRefreshes: bootstrap.invalidateWorkspaceRefreshes,
        refreshGatewayWorkspaceStatuses: bootstrap.refreshGatewayWorkspaceStatuses,
        resetWorkspaceScopedState: refresh.resetWorkspaceScopedState, finishWorkspaceRefresh: refresh.finishWorkspaceRefresh,
        selectionIntentRef, gatewayListRequestRef, runGatewayWorkspaceListTask: queue.enqueue }).activateGatewayWorkspace;
      return null;
    }
    await act(async () => { const renderer = create(<Harness />); mountedRenderers.push(renderer); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(current.activeGatewayWorkspaceId).toBe("ws-old");
    expect(current.sessionsByWorkspace.has("ws-1")).toBe(false);
    activation.mockClear(); lists.mockClear(); snapshots.mockClear();
    await act(async () => { await activate("ws-1"); });
    expect(activation).toHaveBeenCalledTimes(1);
    expect(activation).toHaveBeenCalledWith(API_PORT, "ws-1");
    expect(lists).toHaveBeenCalledTimes(2);
    expect(snapshots).toHaveBeenCalledWith(API_PORT, expect.objectContaining({ workspace_id: "ws-1" }));
    expect(current.activeGatewayWorkspaceId).toBe("ws-1");
    expect(current.workspaceName).toBe("工作区");
    expect(current.sessionsByWorkspace.get("ws-1")).toEqual([]);
    expect(current.workspaceSwitching).toBe(false);
    expect(current.error).toBeNull();
  });

  test("同一活动工作区的旧状态读不能覆盖后发父子关系修改", async () => {
    installWindow(); stubCommonBootstrap();
    spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    const before = gatewayWorkspaceList([{ workspace_id: "ws-1", name: "旧列表", status: "ready" }]);
    const after = gatewayWorkspaceList([{ workspace_id: "ws-1", name: "修改后列表", status: "ready" }]);
    const list = spy("listGatewayWorkspaces").mockResolvedValue(before);
    const mounted = await mountBootstrap(appState());
    let releaseRead!: (value: typeof before) => void;
    list.mockImplementation(() => new Promise((resolve) => { releaseRead = resolve; }));
    const write = spy("updateGatewayWorkspace").mockResolvedValue(after);
    const readOperation = mounted.refreshStatuses("ws-1");
    await act(async () => { await Promise.resolve(); });
    const writeOperation = mounted.setParent("ws-1", "ws-parent");
    expect(write).not.toHaveBeenCalled();
    await act(async () => { releaseRead(before); await readOperation; await writeOperation; });
    expect(mounted.state().gatewayWorkspaces).toBe(after.items);
    expect(mounted.state().activeGatewayWorkspaceId).toBe("ws-1");
    expect(write).toHaveBeenCalledTimes(1);
  });

  test("修改在途时后发状态 GET 必须等真实写入结束并读取新列表", async () => {
    installWindow(); stubCommonBootstrap();
    spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    let authority = gatewayWorkspaceList([{ workspace_id: "ws-1", name: "旧列表", status: "ready" }]);
    const after = gatewayWorkspaceList([{ workspace_id: "ws-1", name: "修改后列表", status: "ready" }]);
    const list = spy("listGatewayWorkspaces").mockImplementation(async () => authority);
    const mounted = await mountBootstrap(appState());
    list.mockClear();
    let releaseWrite!: () => void;
    const write = spy("updateGatewayWorkspace").mockImplementation(async () => {
      await new Promise<void>((resolve) => { releaseWrite = resolve; });
      authority = after;
      return after;
    });
    const writeOperation = mounted.setParent("ws-1", "ws-parent");
    await act(async () => { await Promise.resolve(); });
    const readOperation = mounted.refreshStatuses("ws-1");
    expect(list).not.toHaveBeenCalled();
    await act(async () => { releaseWrite(); await writeOperation; await readOperation; });
    expect(list).toHaveBeenCalledTimes(1);
    expect(write).toHaveBeenCalledTimes(1);
    expect(mounted.state().gatewayWorkspaces).toBe(after.items);
  });

  test("两次修改均执行，后一次失败及对账失败仍保留前一次成功镜像", async () => {
    installWindow(); stubCommonBootstrap();
    spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    const before = gatewayWorkspaceList([{ workspace_id: "ws-1", name: "旧列表", status: "ready" }]);
    const after = gatewayWorkspaceList([{ workspace_id: "ws-1", name: "第一次修改成功", status: "ready" }]);
    const list = spy("listGatewayWorkspaces").mockResolvedValue(before);
    const mounted = await mountBootstrap(appState());
    list.mockRejectedValue(new Error("对账读取失败"));
    let releaseWrite!: (value: typeof after) => void;
    const write = spy("updateGatewayWorkspace").mockImplementationOnce(() => new Promise((resolve) => { releaseWrite = resolve; }))
      .mockRejectedValue(new Error("第二次修改失败"));
    const first = mounted.setParent("ws-1", "ws-parent-1");
    const second = mounted.setParent("ws-1", "ws-parent-2").catch((error: unknown) => error);
    await act(async () => { await Promise.resolve(); releaseWrite(after); await first; await second; });
    expect(write.mock.calls.map((call) => call[2])).toEqual([
      { parent_workspace_id: "ws-parent-1" }, { parent_workspace_id: "ws-parent-2" },
    ]);
    expect(mounted.state().gatewayWorkspaces).toBe(after.items);
    expect(mounted.state().gatewayError).toContain("第二次修改失败");
  });

  test("用户在初始定时器前选择后，旧启动恢复不发出激活副作用", async () => {
    installWindow(); stubCommonBootstrap();
    const list = spy("listGatewayWorkspaces").mockResolvedValue(gatewayWorkspaceList([
      { workspace_id: "ws-1", name: "旧工作区", status: "ready" },
    ]));
    const activate = spy("activateGatewayWorkspace");
    const mounted = await mountBootstrap(appState(), false);
    mounted.selectionIntentRef.current += 1;
    mounted.setLatest({ ...mounted.state(), activeGatewayWorkspaceId: "ws-selected", status: "用户最新选择" });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 0)); });
    expect(activate).not.toHaveBeenCalled();
    expect(list).not.toHaveBeenCalled();
    expect(mounted.state().activeGatewayWorkspaceId).toBe("ws-selected");
    expect(mounted.state().status).toBe("用户最新选择");
  });

  test("旧状态 GET 失败不覆盖同 workspace 的较新会话选择状态", async () => {
    installWindow(); stubCommonBootstrap();
    spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
    const list = spy("listGatewayWorkspaces").mockResolvedValue(gatewayWorkspaceList([
      { workspace_id: "ws-1", name: "工作区", status: "ready" },
    ]));
    const mounted = await mountBootstrap(appState());
    let rejectRead!: (error: Error) => void;
    list.mockImplementation(() => new Promise((_resolve, reject) => { rejectRead = reject; }));
    const operation = mounted.refreshStatuses("ws-1");
    await act(async () => { await Promise.resolve(); });
    mounted.selectionIntentRef.current += 1;
    mounted.setLatest({ ...mounted.state(), status: "用户较新选择", gatewayError: null });
    await act(async () => { rejectRead(new Error("旧读失败")); await operation; });
    expect(mounted.state().status).toBe("用户较新选择");
    expect(mounted.state().gatewayError).toBeNull();
  });
  for (const actualStatus of ["ready", "offline"] as const) {
    test(`缓存 offline 在改变激活目标前核最新健康，实际 ${actualStatus} 沿权威状态选择`, async () => {
      installWindow();
      stubCommonBootstrap();
      spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
      let activating = false;
      let active = "ws-1";
      const checks: boolean[] = [];
      const list = spy("listGatewayWorkspaces").mockImplementation(async (_port, options) => {
        const checkHealth = options?.checkHealth ?? false;
        if (activating) checks.push(checkHealth);
        const result: Awaited<ReturnType<typeof gatewayApi.listGatewayWorkspaces>> = gatewayWorkspaceList([
          { workspace_id: "ws-1", name: "原工作区", status: !activating || checkHealth ? actualStatus : "offline" },
          { workspace_id: "ws-2", name: "另一工作区", status: "ready" },
        ]);
        return { ...result, active_workspace_id: active };
      });
      const activate = spy("activateGatewayWorkspace").mockImplementation(async (_port, workspaceId) => {
        active = workspaceId;
        return {} as never;
      });
      const snapshot = spyOn(workspaceSessionListRefresh, "fetchWorkspaceSessionListSnapshot")
        .mockResolvedValue({ apiPort: API_PORT, workspaceId: "ws-1", generation: 0, sessions: [], complete: true } as never);
      restoreSpies.push(() => snapshot.mockRestore());
      const isCurrent = spyOn(workspaceSessionListRefresh, "isCurrentWorkspaceSessionListSnapshot")
        .mockReturnValue(true);
      restoreSpies.push(() => isCurrent.mockRestore());
      const mounted = await mountBootstrap(appState());
      list.mockClear(); activate.mockClear(); active = "ws-1"; activating = true;
      const selected: Array<string | null> = [];
      await act(async () => {
        selected.push(await mounted.refreshSessions(undefined, {
          checkGatewayWorkspaceHealth: false, reuseCurrentUiSettings: true,
        }));
      });
      expect(checks.slice(0, 2)).toEqual([false, true]);
      expect(selected).toEqual([actualStatus === "ready" ? "ws-1" : "ws-2"]);
      expect(mounted.state().activeGatewayWorkspaceId).toBe(selected[0]);
      expect(activate.mock.calls.map((call) => call[1])).toEqual(actualStatus === "ready" ? [] : ["ws-2"]);
      expect(mounted.state().gatewayWorkspaces.find((workspace) => workspace.workspace_id === "ws-1")?.status).toBe(actualStatus);
    });
  }

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
        complete: true,
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


describe("扩展窗口固定会话 owner 初始化", () => {
  for (const outcome of ["ready", "session-missing", "workspace-offline", "workspace-missing", "catalog-unavailable", "snapshot-stale"] as const) {
    test(`${outcome}: URL 目标优先于当前用户保存的位置且不回退`, async () => {
      installWindow(); stubCommonBootstrap(); installDocumentStub();
      const source = { session_id: "ses_00000000400070008000000000000001", title: "旧会话" } as Session;
      const target = { ...source, session_id: "ses_00000000400070008000000000000002", title: "URL 目标" };
      spy("getGatewayUiSettings").mockResolvedValue(defaultUiSettings() as never);
      spy("listGatewayWorkspaces").mockResolvedValue(gatewayWorkspaceList([
        { workspace_id: "ws-main", name: "原活动工作区", status: "ready" },
        ...(outcome === "workspace-missing" ? [] : [
          { workspace_id: "ws-target", name: "窗口目标", status: outcome === "workspace-offline" ? "offline" : "ready" },
        ]),
      ]));
      const activation = spy("activateGatewayWorkspace");
      const user = spyOn(userAccessApi, "ensureGatewayUserAccess")
        .mockResolvedValue({ kind: "user", user_id: "window-user" } as never);
      const view = spyOn(userViewStateApi, "getLatestGatewayUserViewState").mockResolvedValue({
        user_id: "window-user", workspace_id: "ws-main", session_id: source.session_id,
      } as never);
      const list = spyOn(api, "listSessions").mockResolvedValue({ items: [source], next_cursor: null, has_more: false });
      if (outcome === "catalog-unavailable") list.mockRejectedValue(new Error("目录读取失败"));
      const detail = spyOn(api, "getSession").mockImplementation(async () => {
        if (outcome === "session-missing") throw new HttpRequestError(404, "Not Found", "会话不存在", "/api/v1/sessions/target");
        if (outcome === "snapshot-stale") workspaceSessionListRefresh.invalidateWorkspaceSessionListSnapshot(API_PORT, "ws-target");
        return target;
      });
      restoreSpies.push(() => user.mockRestore(), () => view.mockRestore(), () => list.mockRestore(), () => detail.mockRestore());
      const mounted = await mountBootstrap(appState(), true, { workspaceId: "ws-target", sessionId: target.session_id });
      expect(activation).not.toHaveBeenCalled();
      if (outcome === "ready") {
        expect(mounted.state().currentSession).toEqual(target);
        expect(mounted.state().currentSessionWorkspaceId).toBe("ws-target");
        expect(mounted.state().activeGatewayWorkspaceId).toBe("ws-main");
        expect(mounted.state().error).toBeNull();
      } else {
        expect(mounted.state().currentSession).toBeNull();
        expect(mounted.state().currentSessionWorkspaceId).toBeNull();
        expect(mounted.state().error).toContain(outcome === "session-missing" ? "会话不存在"
          : outcome === "catalog-unavailable" ? "目录读取失败"
          : outcome === "snapshot-stale" ? "会话目录已更新" : "目标工作区不可用");
      }
      if (outcome === "workspace-offline" || outcome === "workspace-missing") {
        expect(detail).not.toHaveBeenCalled();
        expect(list).not.toHaveBeenCalled();
      } else if (outcome === "catalog-unavailable") {
        expect(detail).not.toHaveBeenCalled();
        expect(list).toHaveBeenCalledTimes(1);
      } else {
        expect(detail).toHaveBeenCalledTimes(1);
        expect(detail).toHaveBeenCalledWith(API_PORT, target.session_id, "ws-target", { signal: expect.any(AbortSignal) });
        expect(list).toHaveBeenCalledTimes(1);
        expect(list.mock.calls[0]?.[1]).toBe("ws-target");
      }
    });
  }
});
