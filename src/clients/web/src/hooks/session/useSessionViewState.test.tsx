import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as userViewStateApi from "../../api/gateway/userViewState";
import type {
  GatewayUserAccess,
  GatewayUserViewState,
} from "../../types/backend";
import type { AppState } from "../../types/frontend";
import {
  useSessionViewState,
  type SessionViewStateLoadOutcome,
  type SessionViewStateController,
  type SessionViewStateHost,
} from "./useSessionViewState";

function viewState(overrides: Partial<GatewayUserViewState> = {}): GatewayUserViewState {
  return {
    user_id: "user-view-state",
    workspace_id: "workspace-view-state",
    session_id: "session-view-state",
    turn_anchor: "turn-0",
    scroll_offset: 0,
    follow_latest: true,
    projection_version: 1,
    tool_details_expanded: false,
    updated_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

function access(leaseGeneration = 3): GatewayUserAccess {
  return {
    kind: "user",
    user_id: "user-view-state",
    lease_generation: leaseGeneration,
    expires_at: "2026-09-20T00:00:00Z",
    takeover: false,
  };
}

const host: SessionViewStateHost = {
  apiPort: 49_413,
  currentWorkspaceId: "workspace-view-state",
  currentSessionId: "session-view-state",
  gatewayUserAccess: access(),
  gatewayUserViewStates: new Map(),
  expandDetails: false,
};

const originalFetch = globalThis.fetch;
let renderer: ReactTestRenderer | undefined;
let restoreApi = () => {};

/** 只覆盖本链路读写的状态字段，其余字段对本链路无意义。 */
function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    currentSession: { session_id: "session-view-state" },
    currentSessionWorkspaceId: "workspace-view-state",
    gatewayUserViewStates: new Map(),
    expandDetails: false,
    status: "",
    ...overrides,
  } as unknown as AppState;
}

/** 状态栏写入沿用 hooks.tsx 里 AppProvider 提供的同一个 setStatus。 */
function statusWriter(setState: (update: (previous: AppState) => AppState) => void) {
  return (message: string) => {
    setState((previous) => ({ ...previous, status: message }));
  };
}

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  globalThis.fetch = originalFetch;
  restoreApi();
});

describe("useSessionViewState", () => {
  test("并发加载共享请求，并在成功后完整应用后端对象", async () => {
    let resolveRequest!: (value: GatewayUserViewState) => void;
    let requests = 0;
    const reader = spyOn(userViewStateApi, "getGatewayUserViewState").mockImplementation(
      async () => {
        requests += 1;
        return await new Promise<GatewayUserViewState>((resolve) => {
          resolveRequest = resolve;
        });
      },
    );
    restoreApi = () => reader.mockRestore();

    let controller!: SessionViewStateController;
    let latestState = appState();
    function Probe(): React.ReactNode {
      const [current, setState] = React.useState(appState);
      latestState = current;
      controller = useSessionViewState({
        host,
        setState,
        setStatus: statusWriter(setState),
      });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    const first = controller.loadSessionViewState("workspace-view-state", "session-view-state");
    const second = controller.loadSessionViewState("workspace-view-state", "session-view-state");
    await Promise.resolve();
    expect(requests).toBe(1);

    const loaded = viewState({ tool_details_expanded: true, scroll_offset: 48 });
    resolveRequest(loaded);
    await act(async () => {
      await Promise.all([first, second]);
    });
    // 后端对象整体落到会话 scope，并且当前会话命中时同步工具详情展开态。
    expect(latestState.gatewayUserViewStates.get("workspace-view-state::session-view-state"))
      .toEqual(loaded);
    expect(latestState.expandDetails).toBe(true);
  });

  test("保存响应在 lease generation 变化后不污染当前用户状态", async () => {
    let resolveRequest!: (value: GatewayUserViewState) => void;
    const writer = spyOn(userViewStateApi, "putGatewayUserViewState").mockImplementation(
      async () => await new Promise<GatewayUserViewState>((resolve) => {
        resolveRequest = resolve;
      }),
    );
    restoreApi = () => writer.mockRestore();

    let controller!: SessionViewStateController;
    let latestState = appState();
    let currentHost = { ...host, gatewayUserAccess: access(7) };
    function Probe(): React.ReactNode {
      const [current, setState] = React.useState(appState);
      latestState = current;
      controller = useSessionViewState({
        host: currentHost,
        setState,
        setStatus: statusWriter(setState),
      });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    controller.saveSessionViewState({
      turn_anchor: "turn-1",
      scroll_offset: 10,
      follow_latest: true,
    });
    await Promise.resolve();
    currentHost = { ...currentHost, gatewayUserAccess: access(8) };
    await act(async () => { renderer!.update(<Probe />); });
    resolveRequest(viewState({ scroll_offset: 10 }));
    await act(async () => { await Promise.resolve(); });
    // 迟到响应属于旧 lease：不得写进当前用户的视图状态。
    expect(latestState.gatewayUserViewStates.size).toBe(0);
  });

  test("toggleExpandDetails 写入展开态并触发保存", async () => {
    const writer = spyOn(userViewStateApi, "putGatewayUserViewState").mockImplementation(
      async () => viewState(),
    );
    restoreApi = () => writer.mockRestore();

    let controller!: SessionViewStateController;
    let latestState = appState();
    function Probe(): React.ReactNode {
      const [current, setState] = React.useState(appState);
      latestState = current;
      controller = useSessionViewState({ host, setState, setStatus: statusWriter(setState) });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    act(() => controller.toggleExpandDetails(true));
    await act(async () => { await Promise.resolve(); });

    expect(latestState.expandDetails).toBe(true);
    expect(writer).toHaveBeenCalledTimes(1);
  });

  test("读取失败把原始错误文本写进 status", async () => {
    const reader = spyOn(userViewStateApi, "getGatewayUserViewState").mockImplementation(
      async () => {
        throw new Error("网关视图位置不可读");
      },
    );
    restoreApi = () => reader.mockRestore();

    let controller!: SessionViewStateController;
    let latestState = appState();
    function Probe(): React.ReactNode {
      const [current, setState] = React.useState(appState);
      latestState = current;
      controller = useSessionViewState({ host, setState, setStatus: statusWriter(setState) });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    await act(async () => {
      await controller.loadSessionViewState("workspace-view-state", "session-view-state");
    });

    expect(latestState.status).toBe("读取用户视图位置失败: 网关视图位置不可读");
  });

  test("后端权威返回 null 时删除该会话的陈旧视图状态", async () => {
    const reader = spyOn(userViewStateApi, "getGatewayUserViewState").mockImplementation(
      async () => null,
    );
    restoreApi = () => reader.mockRestore();

    const staleKey = "workspace-view-state::session-view-state";
    let controller!: SessionViewStateController;
    let latestState = appState();
    function Probe(): React.ReactNode {
      const [current, setState] = React.useState(() =>
        appState({ gatewayUserViewStates: new Map([[staleKey, viewState()]]) }),
      );
      latestState = current;
      controller = useSessionViewState({ host, setState, setStatus: statusWriter(setState) });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    await act(async () => {
      await controller.loadSessionViewState("workspace-view-state", "session-view-state");
    });

    // 后端权威地表示「该会话没有保存的视图位置」：陈旧的本地缓存必须被删除，
    // 而不是留成幽灵条目继续影响后续投影。
    expect(latestState.gatewayUserViewStates.has(staleKey)).toBe(false);
    expect(latestState.gatewayUserViewStates.size).toBe(0);
  });
  test("接管换代后上一代 lease 的本地残留不再被应用", async () => {
    const scopeKey = "workspace-view-state::session-view-state";
    // 第 7 代先正常读到一条视图状态，本地因此留有带代际登记的残留。
    const reader = spyOn(userViewStateApi, "getGatewayUserViewState")
      .mockResolvedValueOnce(viewState({ turn_anchor: "turn-stale" }))
      .mockResolvedValueOnce(viewState({ turn_anchor: "turn-new" }));
    restoreApi = () => reader.mockRestore();

    let controller!: SessionViewStateController;
    // host 与 AppState 共用同一份镜像（生产里由 AppProvider 从 state 装配）。
    const sharedMirror = { gatewayUserViewStates: new Map<string, GatewayUserViewState>() };
    let currentHost: SessionViewStateHost = {
      ...host,
      gatewayUserAccess: access(7),
      gatewayUserViewStates: sharedMirror.gatewayUserViewStates,
    };
    let latestState = appState();
    function Probe(): React.ReactNode {
      const [current, setState] = React.useState(appState);
      latestState = current;
      sharedMirror.gatewayUserViewStates = current.gatewayUserViewStates;
      currentHost = { ...currentHost, gatewayUserViewStates: current.gatewayUserViewStates };
      controller = useSessionViewState({
        host: currentHost,
        setState,
        setStatus: statusWriter(setState),
      });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    await act(async () => {
      await controller.loadSessionViewState("workspace-view-state", "session-view-state");
    });
    expect(latestState.gatewayUserViewStates.get(scopeKey)?.turn_anchor).toBe("turn-stale");

    // 用户被接管：user_id 不变，lease 升到第 8 代；后端此时权威地没有视图位置。
    currentHost = { ...currentHost, gatewayUserAccess: access(8) };
    await act(async () => { renderer!.update(<Probe />); });
    await act(async () => {
      await controller.loadSessionViewState("workspace-view-state", "session-view-state");
    });

    // 上一代的残留不得被应用，也不能短路掉读取：必须走后端并接受权威结果。
    expect(reader).toHaveBeenCalledTimes(2);
    expect(latestState.gatewayUserViewStates.get(scopeKey)?.turn_anchor).toBe("turn-new");
  });
  test("权威空与读取失败对调用方可见地区分", async () => {
    const scopeKey = "workspace-view-state::session-view-state";
    const emptyMirror = new Map<string, GatewayUserViewState>();
    let outcome: SessionViewStateLoadOutcome | undefined;

    // 404：后端权威地没有保存视图位置，属于合法空，不是失败。
    const absentReader = spyOn(userViewStateApi, "getGatewayUserViewState")
      .mockResolvedValue(null);
    restoreApi = () => absentReader.mockRestore();
    let controller!: SessionViewStateController;
    function AbsentProbe(): React.ReactNode {
      const [current, setState] = React.useState(appState);
      void current;
      controller = useSessionViewState({
        host: { ...host, gatewayUserViewStates: emptyMirror },
        setState,
        setStatus: statusWriter(setState),
      });
      return null;
    }
    await act(async () => { renderer = create(<AbsentProbe />); });
    await act(async () => {
      outcome = await controller.loadSessionViewState("workspace-view-state", "session-view-state");
    });
    // 权威空：loaded 且 viewState 为 null，不能与失败混淆。
    expect(outcome).toEqual({ kind: "loaded", viewState: null });
    expect(emptyMirror.has(scopeKey)).toBe(false);
    act(() => renderer?.unmount());

    // 500：读取失败必须显式表现为 failed，并带上原始错误。
    const failure = new Error("网关视图位置不可读");
    const failingReader = spyOn(userViewStateApi, "getGatewayUserViewState")
      .mockRejectedValue(failure);
    restoreApi = () => failingReader.mockRestore();
    function FailingProbe(): React.ReactNode {
      const [current, setState] = React.useState(appState);
      void current;
      controller = useSessionViewState({
        host: { ...host, gatewayUserViewStates: new Map<string, GatewayUserViewState>() },
        setState,
        setStatus: statusWriter(setState),
      });
      return null;
    }
    await act(async () => { renderer = create(<FailingProbe />); });
    await act(async () => {
      outcome = await controller.loadSessionViewState("workspace-view-state", "session-view-state");
    });
    expect(outcome).toEqual({ kind: "failed", error: failure });
  });

});
