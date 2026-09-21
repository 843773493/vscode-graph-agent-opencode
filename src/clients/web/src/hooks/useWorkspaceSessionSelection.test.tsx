import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React, { type MutableRefObject } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../api";
import { DEFAULT_BACKEND_PORT } from "../api";
import type { Session } from "../types/backend";
import type { AppState } from "../types/frontend";
import { useWorkspaceSessionSelection } from "./useWorkspaceSessionSelection";

const API_PORT = 49_703;

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function session(sessionId: string): Session {
  return {
    session_id: sessionId,
    title: sessionId,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  } as unknown as Session;
}

/** 只覆盖本链路读写的状态字段，其余字段对本链路无意义。 */
function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    sessionsByWorkspace: new Map(),
    activeGatewayWorkspaceId: null,
    currentSessionWorkspaceId: null,
    workspaceSwitching: false,
    ...overrides,
  } as unknown as AppState;
}

interface MountedHook {
  hook: ReturnType<typeof useWorkspaceSessionSelection>;
  calls: Array<{ kind: string; args: unknown[] }>;
  latestStateRef: MutableRefObject<AppState>;
}

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];

async function mountHook(initialState: AppState): Promise<MountedHook> {
  let hook: ReturnType<typeof useWorkspaceSessionSelection> | undefined;
  const latestStateRef: MutableRefObject<AppState> = { current: initialState };
  const calls: MountedHook["calls"] = [];

  function Probe(): React.ReactNode {
    hook = useWorkspaceSessionSelection({
      apiPort: API_PORT,
      latestStateRef,
      selectSession: (sessionId) => {
        calls.push({ kind: "selectSession", args: [sessionId] });
      },
      selectWorkspaceSession: (workspaceId, sessionId, sessionOverride) => {
        calls.push({ kind: "selectWorkspaceSession", args: [workspaceId, sessionId, sessionOverride] });
      },
      loadSessionViewState: async (workspaceId, sessionId) => {
        calls.push({ kind: "loadSessionViewState", args: [workspaceId, sessionId] });
        return null;
      },
      activateGatewayWorkspaceInBackground: (workspaceId) => {
        calls.push({ kind: "activateGatewayWorkspaceInBackground", args: [workspaceId] });
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);
  return { hook: hook!, calls, latestStateRef };
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restoreSpies.splice(0)) restore();
});

describe("useWorkspaceSessionSelection 顺序语义", () => {
  test("selectSession 先切换会话再加载视图状态，工作区取自最新 state", async () => {
    const { hook, calls, latestStateRef } = await mountHook(
      appState({ currentSessionWorkspaceId: "ws-old", activeGatewayWorkspaceId: "ws-active" }),
    );
    // 模拟选择过程中权威状态已推进，链路必须读 ref 而不是闭包里的旧值。
    latestStateRef.current = appState({ currentSessionWorkspaceId: "ws-new", activeGatewayWorkspaceId: "ws-active" });

    hook.selectSession("session-1");

    expect(calls.map((call) => call.kind)).toEqual(["selectSession", "loadSessionViewState"]);
    expect(calls[1].args).toEqual(["ws-new", "session-1"]);
  });

  test("selectWorkspaceSession 先切换会话再按同一工作区加载视图状态", async () => {
    const { hook, calls } = await mountHook(appState());
    const target = session("session-2");

    hook.selectWorkspaceSession("ws-1", "session-2", target);

    expect(calls.map((call) => call.kind)).toEqual(["selectWorkspaceSession", "loadSessionViewState"]);
    expect(calls[0].args).toEqual(["ws-1", "session-2", target]);
    expect(calls[1].args).toEqual(["ws-1", "session-2"]);
  });
});

describe("useWorkspaceSessionSelection latest-only 队列", () => {
  test("连续打开不同会话时只有最后一次生效", async () => {
    const firstRequest = deferred<Session>();
    const getSession = spyOn(api, "getSession").mockImplementation(
      async (_port, sessionId) => (sessionId === "session-a"
        ? await firstRequest.promise
        : session(sessionId)),
    );
    restoreSpies.push(() => getSession.mockRestore());

    const { hook, calls } = await mountHook(appState());
    hook.openWorkspaceSession("ws-1", "session-a");
    await flush();
    hook.openWorkspaceSession("ws-1", "session-b");

    // 第一个请求返回时它的意图已被第二次打开取代，不得落回选中。
    firstRequest.resolve(session("session-a"));
    await flush();

    expect(getSession).toHaveBeenCalledTimes(2);
    expect(calls.filter((call) => call.kind === "selectWorkspaceSession").map((call) => call.args)).toEqual([
      ["ws-1", "session-b", session("session-b")],
    ]);
  });

  test("缓存命中的会话不再请求后端并切到目标工作区", async () => {
    const getSession = spyOn(api, "getSession").mockResolvedValue(session("unused"));
    restoreSpies.push(() => getSession.mockRestore());
    const cached = session("session-c");

    const { hook, calls } = await mountHook(appState({
      activeGatewayWorkspaceId: "ws-other",
      sessionsByWorkspace: new Map([["ws-1", [cached]]]),
    }));
    hook.openWorkspaceSession("ws-1", "session-c");
    await flush();

    expect(getSession).not.toHaveBeenCalled();
    expect(calls).toContainEqual({ kind: "selectWorkspaceSession", args: ["ws-1", "session-c", cached] });
    expect(calls).toContainEqual({ kind: "activateGatewayWorkspaceInBackground", args: ["ws-1"] });
  });
});
