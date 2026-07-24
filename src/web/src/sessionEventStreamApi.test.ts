import { afterEach, describe, expect, test } from "bun:test";

import {
  SessionStreamIdleTimeoutError,
  streamSessionEvents,
  type SessionStreamEvent,
} from "./api";

const originalFetch = globalThis.fetch;

function localCredentialResponse(port: number) {
  return Response.json({
    data: { token: `test-local-token-${port}` },
    request_id: "req_test",
  });
}

function streamResponse(chunks: string[]): Response {
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
      headers: { "content-type": "text/event-stream" },
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
    agent_id: "default",
    timestamp: "2026-07-24T00:00:00Z",
    type: "job_started",
    raw: {},
  };
}

function traceBlock(event: SessionStreamEvent): string {
  return `id: ${event.event_id}\nevent: trace\ndata: ${JSON.stringify(event)}\n\n`;
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

    await streamSessionEvents(port, "ses_stream_test", {
      afterEventId: "evt_previous",
      idleTimeoutMs: 50,
      onEvent: (event) => {
        received.push(event);
      },
    });

    expect(requestHeaders).toHaveLength(1);
    expect(requestHeaders[0].get("Last-Event-ID")).toBe("evt_previous");
    expect(received.map((event) => event.event_id)).toEqual(["evt_new"]);
  });
});
