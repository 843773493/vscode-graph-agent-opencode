import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { useSessionTraceHistory } from "./useSessionTraceHistory";

const originalFetch = globalThis.fetch;
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
    workspace_id: "workspace-local",
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
        job_id: `job_${eventId}`,
        type: "job_started",
        timestamp: "2026-07-28T00:00:00Z",
        payload: {},
      }],
      next_cursor: null,
      has_more: false,
    },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
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
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const path = new URL(String(args[0])).pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return Response.json({ request_id: "req_token", data: { token: "trace-token" } });
      }
      if (path.includes("session-a")) return oldResponse;
      return tracePage("session-b", "event-b");
    }, { preconnect: originalFetch.preconnect });
    let latestState = state();

    function Harness({ sessionId }: { sessionId: string }): React.ReactNode {
      const [current, setCurrent] = React.useState(state);
      latestState = current;
      const scopeKey = `workspace-a::${sessionId}`;
      useSessionTraceHistory({
        apiPort: port,
        currentSession: session(sessionId),
        workspaceId: "workspace-a",
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
        .get("workspace-a::session-b")?.items.map((event) => event.event_id),
    ).toEqual(["event-b"]);
    expect(
      latestState.sessionTraceHistoryBySession
        .get("workspace-a::session-a")?.items,
    ).toEqual([]);
    renderer!.unmount();
  });

  test("使用服务端 next_cursor 前插更旧页且不写入 live event queue", async () => {
    const port = 49_303;
    installWindow(port);
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const url = new URL(String(args[0]));
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({ request_id: "req_token", data: { token: "page-token" } });
      }
      const older = url.searchParams.get("cursor") === "older-1";
      return Response.json({
        request_id: older ? "req_older" : "req_tail",
        data: {
          items: [{
            event_id: older ? "event-old" : "event-new",
            session_id: "session-page",
            job_id: "job-page",
            type: "job_started",
            timestamp: older ? "2026-07-27T23:59:00Z" : "2026-07-28T00:00:00Z",
            payload: {},
          }],
          next_cursor: older ? null : "older-1",
          has_more: !older,
        },
      });
    }, { preconnect: originalFetch.preconnect });
    let latestState = state();
    let loader: (() => Promise<number>) | null = null;

    function Harness(): React.ReactNode {
      const [current, setCurrent] = React.useState(state);
      latestState = current;
      const scopeKey = "workspace-a::session-page";
      const controller = useSessionTraceHistory({
        apiPort: port,
        currentSession: session("session-page"),
        workspaceId: "workspace-a",
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
        .get("workspace-a::session-page")?.items.map((event) => event.event_id),
    ).toEqual(["event-old", "event-new"]);
    expect(latestState.eventQueuesBySession).toBeUndefined();
    renderer!.unmount();
  });
});
