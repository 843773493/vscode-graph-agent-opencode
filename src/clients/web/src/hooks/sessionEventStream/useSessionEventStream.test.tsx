import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as traceStream from "../../api/stream/sessionTraceStream";
import { TraceCursorGoneError } from "../../api/stream/sessionTraceStream";
import type { AppState } from "../../types/frontend";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS } from "./sessionEventStreamPolicy";
import { useSessionEventStream } from "./useSessionEventStream";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import { flushSessionStreamEventBatch } from "./batchUpdates";
import { INITIAL_APP_STATE } from "../app/appStateSeed";
import type { Session, SessionStreamEvent } from "../../types/backend";
import { useSessionLifecycleActions } from "../session/useSessionLifecycleActions";
import { buildSessionHookState } from "../session/sessionHookTestFixtures";

test("真实 SSE 批次按完整 owner 写附件摘要并排除其它会话的 payload", () => {
  const sessionId = "ses_same_attachments";
  const scope = `gateway_a::${sessionId}`;
  const otherScope = `gateway_b::${sessionId}`;
  let current: AppState = { ...INITIAL_APP_STATE, currentSession: { session_id: sessionId } as AppState["currentSession"],
    currentSessionWorkspaceId: "gateway_a", eventQueuesBySession: new Map(), pendingConversations: new Map(),
    sessionAttachmentSummaries: new Map([[otherScope, { count: 1, names: ["B.txt"], latestAt: null }]]), traceEvents: [] };
  const event = (payloadSession: string, name: string): SessionStreamEvent => ({
    event_id: `event_${payloadSession}`, session_id: sessionId, job_id: "job_attachment", type: "message_created",
    phase: "message", title: "创建消息", content: "", timestamp: "2026-10-14T00:00:00Z",
    raw: { payload: { session_id: payloadSession, attachments: [{ file_id: name, name }] } },
  });
  flushSessionStreamEventBatch([event("ses_foreign", "foreign.txt"), event(sessionId, "A.txt")], {
    apiPort: 49_900, sessionId, workspaceId: "gateway_a", sessionCacheKey: scope,
    setState: (update) => { current = typeof update === "function" ? update(current) : update; },
  });
  expect(current.sessionAttachmentSummaries.get(scope)?.names).toEqual(["A.txt"]);
  expect(current.sessionAttachmentSummaries.get(otherScope)?.names).toEqual(["B.txt"]);
  expect(current.sessionAttachmentSummaries.has(sessionId)).toBe(false);
});

const originalFetch = globalThis.fetch;

function workspaceRequestTarget(workspaceId: string) {
  return {
    workspace_id: workspaceId,
    backend_workspace_id: "00000000-0000-0000-0000-000000000001",
    connection_kind: "local" as const,
  };
}
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

afterEach(() => {
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("document", originalDocumentDescriptor);
});

/** 最小 window/document 桩：定时器压成 0 延迟，避免真实退避把测试拖到分钟级。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void) => globalThis.setTimeout(handler, 0),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
      setInterval: () => 0,
      clearInterval: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      visibilityState: "hidden",
      hasFocus: () => false,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function mountSessionStream(
  state: { value: AppState },
): Promise<ReactTestRenderer> {
  function Probe(): React.ReactNode {
    useSessionEventStream({
      apiPort: 49_902,
      sessionId: "session-reconnect",
      workspaceId: "workspace-reconnect",
      workspaceRequestTarget: workspaceRequestTarget("workspace-reconnect"),
      sessionCacheKey: "workspace-reconnect::session-reconnect",
      activeJobId: null,
      timelineReady: true,
      initialEventCursor: null,
      refreshTurnHistory: () => undefined,
      loadTerminalJobTurn: async () => undefined,
      setState: (update) => {
        state.value = typeof update === "function"
          ? update(state.value)
          : update;
      },
    });
    return null;
  }
  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  return renderer!;
}

function initialAppState(): AppState {
  return { status: "准备就绪" } as unknown as AppState;
}

for (const entry of ["session", "workspace"] as const) {
  test(`${entry} 选择经真实 React 合并 A→B→A 后保留 A 订阅，提交 B 后清理并连接 B`, async () => {
    installWindow();
    const sessionA = { session_id: "ses_selection_a", title: "会话 A" } as Session;
    const sessionB = { session_id: "ses_selection_b", title: "会话 B" } as Session;
    const workspaceA = "gw_selection_a";
    const workspaceB = entry === "workspace" ? "gw_selection_b" : workspaceA;
    const targets = [workspaceRequestTarget(workspaceA), workspaceRequestTarget(workspaceB)];
    const initial = buildSessionHookState({
      workspaceId: workspaceA, current: sessionA, sessions: [sessionA, sessionB],
      gatewayWorkspaces: targets.map((target) => ({
        workspace_id: target.workspace_id, root_path: "/selection", name: target.workspace_id,
      })),
    });
    initial.sessionsByWorkspace.set(workspaceB, workspaceB === workspaceA ? [sessionA, sessionB] : [sessionB]);
    const subscriptions: Array<{
      sessionId: string;
      workspaceId: string | null | undefined;
      signal: AbortSignal;
      onEvent: NonNullable<NonNullable<Parameters<typeof traceStream.streamSessionEvents>[2]>["onEvent"]>;
    }> = [];
    const streamSpy = spyOn(traceStream, "streamSessionEvents").mockImplementation(
      async (_port, sessionId, options) => {
        if (!options?.signal || !options.onEvent) throw new Error("真实订阅缺少 signal 或事件消费者");
        subscriptions.push({ sessionId, workspaceId: options.workspaceId,
          signal: options.signal, onEvent: options.onEvent });
        await new Promise<void>((resolve) => options.signal!.addEventListener("abort", () => resolve(), { once: true }));
      },
    );
    let latest = initial;
    let actions: ReturnType<typeof useSessionLifecycleActions> | null = null;
    let invalidations = 0;
    const invalidateAgentState = () => { invalidations += 1; };
    const refreshTurnHistory = () => undefined;
    const loadTerminalJobTurn = async () => undefined;
    function Harness(): React.ReactNode {
      const [current, setState] = React.useState(initial);
      const currentRef = React.useRef(current);
      currentRef.current = current;
      latest = current;
      const readState = React.useCallback(() => currentRef.current, []);
      const selectionIntentRef = React.useRef(0);
      const workspaceId = current.currentSessionWorkspaceId ?? current.activeGatewayWorkspaceId;
      const { abortCurrentStream } = useSessionEventStream({
        apiPort: 49_908, sessionId: current.currentSession?.session_id ?? null,
        workspaceId, workspaceRequestTarget: targets.find((target) => target.workspace_id === workspaceId) ?? null,
        sessionCacheKey: `${workspaceId}::${current.currentSession?.session_id}`,
        activeJobId: null, timelineReady: true, initialEventCursor: null,
        refreshTurnHistory, loadTerminalJobTurn, setState,
      });
      actions = useSessionLifecycleActions({
        apiPort: 49_908, currentSession: current.currentSession,
        activeGatewayWorkspaceId: current.activeGatewayWorkspaceId,
        currentSessionGatewayWorkspaceId: workspaceId,
        defaultGatewayWorkspaceId: workspaceA, workspaceRequestTargets: targets,
        readState, setState, abortCurrentStream, invalidateAgentState, selectionIntentRef,
      });
      return null;
    }
    let renderer: ReactTestRenderer | undefined;
    const select = (workspaceId: string, sessionId: string) => {
      if (!actions) throw new Error("生命周期动作尚未挂载");
      if (entry === "session") actions.selectSession(sessionId);
      else actions.selectWorkspaceSession(workspaceId, sessionId);
    };
    try {
      act(() => { renderer = create(<Harness />); });
      await flush();
      expect(subscriptions).toHaveLength(1);
      const original = subscriptions[0];
      act(() => {
        select(workspaceB, sessionB.session_id);
        select(workspaceA, sessionA.session_id);
      });
      await flush();
      expect(latest.currentSession?.session_id).toBe(sessionA.session_id);
      expect(latest.currentSessionWorkspaceId).toBe(workspaceA);
      expect(original.signal.aborted).toBe(false);
      expect(subscriptions).toHaveLength(1);
      expect(invalidations).toBe(0);
      act(() => original.onEvent({
        event_id: "evt_selection_a", session_id: sessionA.session_id, job_id: "job_selection_a",
        type: "model_text_delta", phase: "model", title: "模型增量", content: "A 仍接收事件",
        timestamp: "2026-10-14T00:00:00Z", raw: {},
      }, "cursor_selection_a"));
      await flush();
      expect(latest.eventQueuesBySession.get(`${workspaceA}::${sessionA.session_id}`)?.some(
        (event) => event.kind === "trace" && event.event.event_id === "evt_selection_a",
      )).toBe(true);
      act(() => select(workspaceB, sessionB.session_id));
      await flush();
      expect(original.signal.aborted).toBe(true);
      expect(subscriptions).toHaveLength(2);
      expect(subscriptions[1].sessionId).toBe(sessionB.session_id);
      expect(subscriptions[1].workspaceId).toBe(workspaceB);
      expect(invalidations).toBe(1);
      act(() => renderer!.unmount());
      renderer = undefined;
      expect(subscriptions[1].signal.aborted).toBe(true);
    } finally {
      if (renderer) act(() => renderer!.unmount());
      streamSpy.mockRestore();
    }
  });
}

describe("useSessionEventStream readiness", () => {
  test("Turn projection 尚未 ready 时不请求会话 Trace stream", async () => {
    let fetchCount = 0;
    globalThis.fetch = Object.assign(async () => {
      fetchCount += 1;
      throw new Error("partial 阶段不应连接网络");
    }, { preconnect: originalFetch.preconnect });

    function Harness(): React.ReactNode {
      useSessionEventStream({
        apiPort: 49_401,
        sessionId: "session-partial",
        workspaceId: "workspace-partial",
        workspaceRequestTarget: workspaceRequestTarget("workspace-partial"),
        sessionCacheKey: "workspace-partial::session-partial",
        activeJobId: null,
        timelineReady: false,
        initialEventCursor: null,
        refreshTurnHistory: () => undefined,
        loadTerminalJobTurn: async () => undefined,
        setState: (update) => {
          void update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await Promise.resolve();
    });

    expect(fetchCount).toBe(0);
    act(() => renderer!.unmount());
  });
});

describe("useSessionEventStream 有界重连", () => {
  test("连续建连失败到达上限后停止重连并给出可见终态", async () => {
    installWindow();
    const streamSpy = spyOn(traceStream, "streamSessionEvents")
      .mockRejectedValue(new Error("连接被拒绝"));
    const state = { value: initialAppState() };
    const renderer = await mountSessionStream(state);

    // 冲刷远超上限的轮次：若上限判定缺失，重连次数会继续无界增长。
    for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 10; round += 1) {
      await flush();
    }

    expect(streamSpy).toHaveBeenCalledTimes(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(state.value.status).toContain("已停止自动重连");
    streamSpy.mockRestore();
    act(() => renderer.unmount());
  });

  test("连接建立并持续有心跳活动时不会被上限误判为放弃", async () => {
    installWindow();
    // 每次建连都立即报告活动（等价于收到心跳注释），随后断开重连；
    // onActivity 归零计数，因此连续 20 次「先建立后断开」必须一直重连。
    const streamSpy = spyOn(traceStream, "streamSessionEvents")
      .mockImplementation(async (_port, _sessionId, options) => {
        options?.onActivity?.();
        throw new Error("断开");
      });
    const state = { value: initialAppState() };
    const renderer = await mountSessionStream(state);

    for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 14; round += 1) {
      await flush();
    }

    expect(streamSpy.mock.calls.length)
      .toBeGreaterThan(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(state.value.status).not.toContain("已停止自动重连");
    streamSpy.mockRestore();
    act(() => renderer.unmount());
  });
});

describe("useSessionEventStream 有界游标失效恢复", () => {
  test("上游持续 410 时停在上限并给出可见终态，不再无上限重建事件流", async () => {
    installWindow();
    const streamSpy = spyOn(traceStream, "streamSessionEvents")
      .mockRejectedValue(new TraceCursorGoneError("stale-cursor"));
    const state = { value: initialAppState() };

    // 410 恢复会调用 refreshTurnHistory，真实链路里该调用会重载 Turn bootstrap
    // 并因 timelineReady 变化而重建事件流 effect。这里用 ready 的收敛来回放同一
    // 序列：若游标失效计数只放局部变量，每次 effect 重建都会把它清零。
    function Probe(): React.ReactNode {
      const [timelineReady, setTimelineReady] = React.useState(true);
      const refreshTurnHistory = React.useCallback(() => {
        setTimelineReady(false);
        globalThis.setTimeout(() => {
          setTimelineReady(true);
        }, 0);
      }, []);
      useSessionEventStream({
        apiPort: 49_903,
        sessionId: "session-cursor-gone",
        workspaceId: "workspace-cursor-gone",
        workspaceRequestTarget: workspaceRequestTarget("workspace-cursor-gone"),
        sessionCacheKey: "workspace-cursor-gone::session-cursor-gone",
        activeJobId: null,
        timelineReady,
        initialEventCursor: "stale-cursor",
        refreshTurnHistory,
        loadTerminalJobTurn: async () => undefined,
        setState: (update) => {
          state.value = typeof update === "function"
            ? update(state.value)
            : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer | undefined;
    await act(async () => {
      renderer = create(<Probe />);
    });

    // 冲刷远超上限的轮次：若游标失效计数缺失，重建次数会随轮次线性无界增长。
    for (let round = 0; round < 40; round += 1) {
      await flush();
    }

    expect(streamSpy.mock.calls.length)
      .toBe(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS);
    expect(state.value.status).toContain("已停止自动恢复");
    streamSpy.mockRestore();
    act(() => renderer!.unmount());
  });

  test("连接建立并收到活动后游标失效计数归零，恢复重新获得完整预算", async () => {
    installWindow();
    let connectedCount = 0;
    // 每轮先真正建立连接（上报活动）再以 410 断开：onActivity 归零计数，
    // 因此只要持续有活动，就不能被误判为停止自动恢复。
    const streamSpy = spyOn(traceStream, "streamSessionEvents")
      .mockImplementation(async (_port, _sessionId, options) => {
        options?.onActivity?.();
        connectedCount += 1;
        throw new TraceCursorGoneError("stale-cursor");
      });
    const state = { value: initialAppState() };

    function Probe(): React.ReactNode {
      const [timelineReady, setTimelineReady] = React.useState(true);
      const refreshTurnHistory = React.useCallback(() => {
        setTimelineReady(false);
        globalThis.setTimeout(() => setTimelineReady(true), 0);
      }, []);
      useSessionEventStream({
        apiPort: 49_904,
        sessionId: "session-cursor-gone-active",
        workspaceId: "workspace-cursor-gone-active",
        workspaceRequestTarget: workspaceRequestTarget("workspace-cursor-gone-active"),
        sessionCacheKey: "workspace-cursor-gone-active::session-cursor-gone-active",
        activeJobId: null,
        timelineReady,
        initialEventCursor: "stale-cursor",
        refreshTurnHistory,
        loadTerminalJobTurn: async () => undefined,
        setState: (update) => {
          state.value = typeof update === "function"
            ? update(state.value)
            : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer | undefined;
    await act(async () => {
      renderer = create(<Probe />);
    });

    for (let round = 0; round < 30; round += 1) {
      await flush();
    }

    expect(connectedCount)
      .toBeGreaterThan(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(state.value.status).not.toContain("已停止自动恢复");
    streamSpy.mockRestore();
    act(() => renderer!.unmount());
  });
});
