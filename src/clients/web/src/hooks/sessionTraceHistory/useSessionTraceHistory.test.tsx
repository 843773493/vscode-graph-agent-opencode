import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { useSessionTraceHistory } from "./useSessionTraceHistory";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "../../api/session/sessionApiFetchMock";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const OWNER = { gatewayId: "gateway-trace-history", workspaceId: "00000000-0000-4000-8000-000000000072" };
const WORKSPACE_KEY = gatewayWorkspaceKey(OWNER);
const originalFetch = globalThis.fetch;
const businessRequests: string[] = [];
let expectsBusinessRequest = false;

function installTraceBackend(handler: Parameters<typeof installSessionApiFetchMock>[0]): void {
  expectsBusinessRequest = true;
  installSessionApiFetchMock((request) => {
    expect(/^\/api\/v1\/sessions\/[^/]+\/traces$/.test(request.path)).toBe(true);
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNER.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNER.workspaceId);
    businessRequests.push(request.path);
    return handler(request);
  });
}
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

function session(sessionId: string): Session {
  return {
    session_id: sessionId,
    workspace_id: OWNER.workspaceId,
    resource_identity: `fixture-${sessionId}`,
    uri: `boxteam://workspace/${OWNER.workspaceId}/resources/session/${sessionId}`,
    scope: "workspace",
    title: sessionId,
    current_agent_id: "default",
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
  };
}

function state(): AppState {
  return {
    sessionTraceHistoryBySession: new Map(),
    status: "",
  } as AppState;
}

function tracePage(sessionId: string, eventId: string) {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: `req_${eventId}`,
    data: {
      items: [{
        event_id: eventId,
        session_id: sessionId,
        thread_id: "thr_00000000700070008000000000000001",
        job_id: `job_${eventId}`,
        type: "job_started",
        phase: "job",
        title: "任务开始",
        content: "",
        skill_names: [],
        timestamp: "2026-07-28T00:00:00Z",
        payload: {},
      }],
      next_cursor: null,
      has_more: false,
    },
  });
}

afterEach(() => {
  unwrapSessionApiFetch();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  const count = businessRequests.length;
  const expected = expectsBusinessRequest;
  businessRequests.length = 0;
  expectsBusinessRequest = false;
  if (expected) expect(count).toBeGreaterThan(0);
});

describe("useSessionTraceHistory", () => {
  test("事件视图未打开时不请求 Trace 历史", async () => {
    const port = 49_301;
    installWindow(port);
    let fetchCount = 0;
    globalThis.fetch = Object.assign(async () => {
      fetchCount += 1;
      return tracePage("session-a", "event-a");
    }, { preconnect: originalFetch.preconnect });

    function Harness(): React.ReactNode {
      const [current, setCurrent] = React.useState(state);
      useSessionTraceHistory({
        apiPort: port,
        currentSession: session("session-a"),
        workspaceId: "workspace-a",
        scopeKey: "workspace-a::session-a",
        active: false,
        history: current.sessionTraceHistoryBySession.get("workspace-a::session-a") ?? null,
        setState: setCurrent,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await Promise.resolve();
    });
    expect(fetchCount).toBe(0);
    renderer!.unmount();
  });

  test("切换会话后丢弃迟到旧响应并按 scope 保存新尾页", async () => {
    const port = 49_302;
    installWindow(port);
    let resolveOld: (response: Response) => void = () => undefined;
    const oldResponse = new Promise<Response>((resolve) => { resolveOld = resolve; });
    installTraceBackend(async ({ url: rawUrl }) => {
      // window 桩只提供 location.port，getApiBaseUrl() 因此返回同源相对路径；
      // 必须显式给出 base，否则 new URL(相对路径) 会抛错并让被测 hook 看不到响应。
      const path = new URL(rawUrl, `http://127.0.0.1:${port}`).pathname;
      if (path.includes("session-a")) return oldResponse;
      return tracePage("session-b", "event-b");
    });
    let latestState = state();

    function Harness({ sessionId }: { sessionId: string }): React.ReactNode {
      const [current, setCurrent] = React.useState(state);
      latestState = current;
      const scopeKey = sessionScopeKey(WORKSPACE_KEY, sessionId);
      useSessionTraceHistory({
        apiPort: port,
        currentSession: session(sessionId),
        workspaceId: WORKSPACE_KEY,
        scopeKey,
        active: true,
        history: current.sessionTraceHistoryBySession.get(scopeKey) ?? null,
        setState: setCurrent,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness sessionId="session-a" />);
      await Promise.resolve();
    });
    await act(async () => {
      renderer!.update(<Harness sessionId="session-b" />);
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      resolveOld(tracePage("session-a", "event-a"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(
      latestState.sessionTraceHistoryBySession
        .get(sessionScopeKey(WORKSPACE_KEY, "session-b"))?.items.map((event) => event.event_id),
    ).toEqual(["event-b"]);
    expect(
      latestState.sessionTraceHistoryBySession
        .get(sessionScopeKey(WORKSPACE_KEY, "session-a"))?.items,
    ).toEqual([]);
    renderer!.unmount();
  });

  test("切回已完成的事件历史缓存不再请求 Trace", async () => {
    const port = 49_304;
    installWindow(port);
    let traceFetchCount = 0;
    installTraceBackend(async ({ url: rawUrl }) => {
      const path = new URL(rawUrl, `http://127.0.0.1:${port}`).pathname;
      if (path.endsWith("/traces")) traceFetchCount += 1;
      return tracePage("session-cached", `event-${traceFetchCount}`);
    });

    function Harness({ active }: { active: boolean }): React.ReactNode {
      const [current, setCurrent] = React.useState(state);
      const scopeKey = sessionScopeKey(WORKSPACE_KEY, "session-cached");
      useSessionTraceHistory({
        apiPort: port,
        currentSession: session("session-cached"),
        workspaceId: WORKSPACE_KEY,
        scopeKey,
        active,
        history: current.sessionTraceHistoryBySession.get(scopeKey) ?? null,
        setState: setCurrent,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness active />);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(traceFetchCount).toBe(1);

    await act(async () => {
      renderer!.update(<Harness active={false} />);
      await Promise.resolve();
    });
    await act(async () => {
      renderer!.update(<Harness active />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(traceFetchCount).toBe(1);
    renderer!.unmount();
  });

  test("使用服务端 next_cursor 前插更旧页且不写入 live event queue", async () => {
    const port = 49_303;
    installWindow(port);
    installTraceBackend(async ({ url: rawUrl }) => {
      const url = new URL(rawUrl, `http://127.0.0.1:${port}`);
      const older = url.searchParams.get("cursor") === "older-1";
      return Response.json({
        request_id: older ? "req_older" : "req_tail",
        data: {
          items: [{
            event_id: older ? "event-old" : "event-new",
            session_id: "session-page",
            thread_id: "thr_00000000700070008000000000000001",
            job_id: "job-page",
            type: "job_started",
            phase: "job",
            title: "任务开始",
            content: "",
            skill_names: [],
            timestamp: older ? "2026-07-27T23:59:00Z" : "2026-07-28T00:00:00Z",
            payload: {},
          }],
          next_cursor: older ? null : "older-1",
          has_more: !older,
        },
      });
    });
    let latestState = state();
    let loader: (() => Promise<number>) | null = null;

    function Harness(): React.ReactNode {
      const [current, setCurrent] = React.useState(state);
      latestState = current;
      const scopeKey = sessionScopeKey(WORKSPACE_KEY, "session-page");
      const controller = useSessionTraceHistory({
        apiPort: port,
        currentSession: session("session-page"),
        workspaceId: WORKSPACE_KEY,
        scopeKey,
        active: true,
        history: current.sessionTraceHistoryBySession.get(scopeKey) ?? null,
        setState: setCurrent,
      });
      loader = controller.loadOlder;
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await Promise.resolve();
      await Promise.resolve();
    });
    let added = 0;
    await act(async () => {
      added = await loader!();
      await Promise.resolve();
    });

    expect(added).toBe(1);
    expect(
      latestState.sessionTraceHistoryBySession
        .get(sessionScopeKey(WORKSPACE_KEY, "session-page"))?.items.map((event) => event.event_id),
    ).toEqual(["event-old", "event-new"]);
    expect(latestState.eventQueuesBySession).toBeUndefined();
    renderer!.unmount();
  });
  test("同一会话的 Thread 切换丢弃旧响应，尾页与旧页均发送精确 Thread", async () => {
    const port = 49_305;
    installWindow(port);
    const threadA = "thr_00000000700070008000000000000001";
    const threadB = "thr_00000000700070008000000000000002";
    const requests: { thread: string | null; cursor: string | null }[] = [];
    let resolveA!: (response: Response) => void;
    const pendingA = new Promise<Response>(resolve => { resolveA = resolve; });
    installTraceBackend(async ({ url: rawUrl }) => {
      const url = new URL(rawUrl, `http://127.0.0.1:${port}`);
      const thread = url.searchParams.get("thread_id");
      const cursor = url.searchParams.get("cursor");
      requests.push({ thread, cursor });
      if (thread === threadA) return pendingA;
      const page = await tracePage("session-shared", cursor ? "event-b-old" : "event-b-new").json();
      page.data.items[0].thread_id = threadB;
      page.data.next_cursor = cursor ? null : "older-b";
      page.data.has_more = !cursor;
      return Response.json(page);
    });
    let latestState = state();
    let loadOlder!: () => Promise<number>;
    function Harness({ threadId }: { threadId: string }): React.ReactNode {
      const [current, setCurrent] = React.useState(state);
      latestState = current;
      const scopeKey = `${sessionScopeKey(WORKSPACE_KEY, "session-shared")}::${threadId}`;
      const controller = useSessionTraceHistory({ apiPort: port, currentSession: session("session-shared"),
        workspaceId: WORKSPACE_KEY, threadId, scopeKey, active: true,
        history: current.sessionTraceHistoryBySession.get(scopeKey) ?? null, setState: setCurrent });
      loadOlder = controller.loadOlder;
      return null;
    }
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<Harness threadId={threadA} />); });
    await act(async () => { renderer.update(<Harness threadId={threadB} />); });
    await act(async () => { resolveA(tracePage("session-shared", "event-a-late")); });
    await act(async () => { expect(await loadOlder()).toBe(1); });
    expect(requests).toEqual([{ thread: threadA, cursor: null }, { thread: threadB, cursor: null }, { thread: threadB, cursor: "older-b" }]);
    expect(latestState.sessionTraceHistoryBySession.get(`${sessionScopeKey(WORKSPACE_KEY, "session-shared")}::${threadA}`)?.items).toEqual([]);
    expect(latestState.sessionTraceHistoryBySession.get(`${sessionScopeKey(WORKSPACE_KEY, "session-shared")}::${threadB}`)?.items.map(item => item.event_id))
      .toEqual(["event-b-old", "event-b-new"]);
    renderer.unmount();
  });

});
