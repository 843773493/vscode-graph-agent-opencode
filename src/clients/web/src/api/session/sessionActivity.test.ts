import { afterEach, describe, expect, jest, test } from "bun:test";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
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
  jest.useRealTimers();
  globalThis.fetch = originalFetch;
});

async function flushMicrotasks(rounds = 50): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
  }
}

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

describe("Workspace 会话活动流空闲超时", () => {
  test("达到统一空闲阈值才响亮失败，不再无限静默挂起", async () => {
    // 活动流与其它长连 SSE 流共用全仓唯一阈值；该断言把口径钉死在代码里。
    expect(SSE_IDLE_TIMEOUT_MS).toBeGreaterThan(15_000);

    const port = 48_204;
    let streamCancelled = false;
    let requestCount = 0;
    globalThis.fetch = Object.assign(async () => {
      requestCount += 1;
      if (requestCount === 1) {
        return Response.json({ data: { token: "activity-token" } });
      }
      return new Response(new ReadableStream<Uint8Array>({
        start() {},
        cancel() {
          streamCancelled = true;
        },
      }), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }, { preconnect: originalFetch.preconnect });

    jest.useFakeTimers();
    const settled: { outcome: string | null; failure: Error | null } = {
      outcome: null,
      failure: null,
    };
    // 不 await：超时丢失时 promise 永不 settle，轮询终态把「挂起」本身变成失败。
    streamSessionActivity(port, "workspace-1", { after: 0 }).then(
      () => {
        settled.outcome = "resolved";
      },
      (error: unknown) => {
        settled.outcome = "rejected";
        settled.failure = error instanceof Error ? error : new Error(String(error));
      },
    );
    await flushMicrotasks();

    // 阈值前必须仍然在等：健康的慢流不会被提前判死。
    jest.advanceTimersByTime(SSE_IDLE_TIMEOUT_MS - 1);
    await flushMicrotasks();
    expect(settled.outcome).toBeNull();

    jest.advanceTimersByTime(1);
    await flushMicrotasks();
    expect(settled.outcome).toBe("rejected");
    expect(settled.failure?.message).toContain("未收到任何数据");
    expect(streamCancelled).toBe(true);
  });
});
