import { afterEach, describe, expect, test } from "bun:test";

import {
  listSessionTraceHistory,
  SessionStreamIdleTimeoutError,
  streamSessionEvents,
  TraceCursorGoneError,
} from "./sessionTraceStream";
import type { SessionStreamEvent } from "../types/backend";

const originalFetch = globalThis.fetch;

function localCredentialResponse(port: number) {
  return Response.json({
    data: { token: `test-local-token-${port}` },
    request_id: "req_test",
  });
}

function streamResponse(
  chunks: string[],
  headers: Record<string, string> = {},
): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    }),
    {
      status: 200,
      headers: { "content-type": "text/event-stream", ...headers },
    },
  );
}

function installStreamBackend(
  port: number,
  responseFactory: () => Response,
): Headers[] {
  let requestCount = 0;
  const streamRequestHeaders: Headers[] = [];
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const [, init] = args;
      requestCount += 1;
      if (requestCount === 1) {
        return localCredentialResponse(port);
      }
      streamRequestHeaders.push(new Headers(init?.headers));
      return responseFactory();
    },
    { preconnect: originalFetch.preconnect },
  );
  return streamRequestHeaders;
}

function traceEvent(eventId: string): SessionStreamEvent {
  return {
    event_id: eventId,
    part_id: null,
    session_id: "ses_stream_test",
    job_id: "job_stream_test",
    step_id: null,
    timestamp: "2026-07-24T00:00:00Z",
    type: "job_started",
    phase: "job",
    title: "任务已开始",
    content: "任务已开始执行",
    raw: { agent_id: "default", payload: {} },
  };
}

function traceBlock(
  event: SessionStreamEvent,
  cursor: string = `tc1.${event.event_id}`,
): string {
  return `id: ${cursor}\nevent: trace\ndata: ${JSON.stringify(event)}\n\n`;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("会话 SSE 客户端", () => {
  test("心跳只刷新连接活跃时间而不产生业务事件", async () => {
    const port = 49_001;
    installStreamBackend(port, () => streamResponse([": heartbeat\n\n"]));
    let activityCount = 0;
    const received: SessionStreamEvent[] = [];

    await streamSessionEvents(port, "ses_heartbeat_test", {
      idleTimeoutMs: 50,
      onActivity: () => {
        activityCount += 1;
      },
      onEvent: (event) => {
        received.push(event);
      },
    });

    expect(activityCount).toBe(1);
    expect(received).toEqual([]);
  });

  test("无字节的半死连接触发空闲超时并取消旧响应流", async () => {
    const port = 49_002;
    let streamCancelled = false;
    installStreamBackend(
      port,
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            cancel() {
              streamCancelled = true;
            },
          }),
          {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          },
        ),
    );

    await expect(
      streamSessionEvents(port, "ses_idle_test", { idleTimeoutMs: 10 }),
    ).rejects.toBeInstanceOf(SessionStreamIdleTimeoutError);
    expect(streamCancelled).toBe(true);
  });

  test("重连请求携带 Last-Event-ID 且只交付服务端返回的新事件", async () => {
    const port = 49_003;
    const newEvent = traceEvent("evt_new");
    const requestHeaders = installStreamBackend(
      port,
      () => streamResponse([traceBlock(newEvent)]),
    );
    const received: SessionStreamEvent[] = [];
    const receivedCursors: string[] = [];

    await streamSessionEvents(port, "ses_stream_test", {
      afterCursor: "evt_previous",
      idleTimeoutMs: 50,
      onEvent: (event, cursor) => {
        received.push(event);
        receivedCursors.push(cursor);
      },
    });

    expect(requestHeaders).toHaveLength(1);
    expect(requestHeaders[0].get("Last-Event-ID")).toBe("evt_previous");
    expect(received.map((event) => event.event_id)).toEqual(["evt_new"]);
    expect(receivedCursors).toEqual(["tc1.evt_new"]);
  });

  test("连接建立时暴露 Gateway 工作区路由代次", async () => {
    const port = 49_004;
    installStreamBackend(
      port,
      () => streamResponse(
        [": heartbeat\n\n"],
        { "X-BoxTeam-Route-Revision": "gw_stream:7" },
      ),
    );
    let routeRevision = "";

    await streamSessionEvents(port, "ses_stream_test", {
      idleTimeoutMs: 50,
      onConnected: (value) => {
        routeRevision = value ?? "";
      },
    });

    expect(routeRevision).toBe("gw_stream:7");
  });

  test("事件历史按 opaque cursor 向旧页分页", async () => {
    const port = 49_005;
    const requestedUrls: string[] = [];
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const url = new URL(String(args[0]));
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return localCredentialResponse(port);
      }
      if (url.pathname === "/api/gateway/users/current") {
        return Response.json({
          data: { user_id: "test-user" },
          request_id: "req_test_user",
        });
      }
      requestedUrls.push(`${url.pathname}${url.search}`);
      return Response.json({
        request_id: "req_trace_page",
        data: { items: [], next_cursor: "older-2", has_more: true },
      });
    }, { preconnect: originalFetch.preconnect });

    const page = await listSessionTraceHistory(
      port,
      "ses_stream_test",
      "gw_stream_test",
      { cursor: "opaque cursor", limit: 100 },
    );

    expect(requestedUrls).toEqual([
      "/api/v1/sessions/ses_stream_test/traces?limit=100&cursor=opaque+cursor",
    ]);
    expect(page.next_cursor).toBe("older-2");
    expect(page.has_more).toBe(true);
  });

  test("历史 Trace 游标失效时抛出可恢复的专用错误", async () => {
    const port = 49_006;
    let requestCount = 0;
    globalThis.fetch = Object.assign(
      async () => {
        requestCount += 1;
        if (requestCount === 1) {
          return localCredentialResponse(port);
        }
        return Response.json(
          { detail: { code: "trace_cursor_gone" } },
          { status: 410, statusText: "Gone" },
        );
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(
      listSessionTraceHistory(
        port,
        "ses_stream_test",
        "gw_stream_test",
        { cursor: "evt_expired" },
      ),
    ).rejects.toBeInstanceOf(TraceCursorGoneError);
  });
});
