import { afterEach, describe, expect, test } from "bun:test";
import {
  listSessionActivity,
  streamSessionActivity,
  SessionActivityCursorGoneError,
} from "./sessionActivity";

const originalFetch = globalThis.fetch;

function streamResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), {
    status,
    headers: { "content-type": "text/event-stream" },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("Workspace 会话活动 API", () => {
  test("列表请求携带工作区并返回持久游标", async () => {
    let request: RequestInit | undefined;
    let count = 0;
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      count += 1;
      if (count === 1) return Response.json({ data: { token: "activity-token" } });
      request = args[1];
      return Response.json({
        data: {
          items: [{
            event_seq: 7,
            event_id: "evt-7",
            session_id: "session-7",
            status: "completed",
            summary: "任务完成",
            occurred_at: "2026-08-16T00:00:00Z",
          }],
          next_cursor: null,
          has_more: false,
        },
        request_id: "req-activity",
      });
    }, { preconnect: originalFetch.preconnect });

    const page = await listSessionActivity(48_201, "workspace-1", { after: 6 });
    expect(page.items[0]?.event_seq).toBe(7);
    expect(new Headers(request?.headers).get("X-BoxTeam-Workspace-Id"))
      .toBe("workspace-1");
  });

  test("SSE 活动事件解析 id 并转发游标", async () => {
    let count = 0;
    globalThis.fetch = Object.assign(async () => {
      count += 1;
      if (count === 1) return Response.json({ data: { token: "activity-token" } });
      return streamResponse([
        "id: 8\nevent: session_activity\ndata: {\"event_seq\":8,\"event_id\":\"evt-8\",\"session_id\":\"session-8\",\"status\":\"failed\",\"summary\":\"任务失败\",\"occurred_at\":\"2026-08-16T00:00:00Z\"}\n\n",
      ]);
    }, { preconnect: originalFetch.preconnect });
    const received: number[] = [];

    await streamSessionActivity(48_202, "workspace-1", {
      after: 7,
      onEvent: (event, cursor) => {
        expect(event.session_id).toBe("session-8");
        received.push(cursor);
      },
    });
    expect(received).toEqual([8]);
  });

  test("游标失效直接暴露给调用方", async () => {
    let count = 0;
    globalThis.fetch = Object.assign(async () => {
      count += 1;
      if (count === 1) return Response.json({ data: { token: "activity-token" } });
      return new Response("{}", { status: 410, statusText: "Gone" });
    }, { preconnect: originalFetch.preconnect });

    await expect(
      streamSessionActivity(48_203, "workspace-1", { after: 3 }),
    ).rejects.toBeInstanceOf(SessionActivityCursorGoneError);
  });
});
