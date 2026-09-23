import { afterEach, describe, expect, jest, test } from "bun:test";

import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import { streamSessionMessageEvents } from "./sessionMessageStream";

const originalFetch = globalThis.fetch;

afterEach(() => {
  jest.useRealTimers();
  globalThis.fetch = originalFetch;
});

/** 只认领本地凭据与消息流两条隧道，其余请求响亮失败。 */
function installStreamBackend(
  port: number,
  responseFactory: () => Response,
): void {
  let requestCount = 0;
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      requestCount += 1;
      if (requestCount === 1) {
        return Response.json({ data: { token: `test-token-${port}` }, request_id: "req_test" });
      }
      return responseFactory();
    },
    { preconnect: originalFetch.preconnect },
  );
}

async function flushMicrotasks(rounds = 50): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
  }
}

function sseInit(): ResponseInit {
  return { status: 200, headers: { "content-type": "text/event-stream" } };
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
    sseInit(),
  );
}

function messageEvent(eventSeq: number) {
  return {
    event_id: `evt_message_${eventSeq}`,
    session_id: "ses_message_test",
    turn_id: "turn_message_test",
    turn_stream_id: "stream_message_test",
    event_seq: eventSeq,
    type: "stream.opened",
    payload: { status: "open" },
  };
}

function messageBlock(frameId: string, eventSeq: number): string {
  return `id: ${frameId}\nevent: stream.opened\ndata: ${JSON.stringify(messageEvent(eventSeq))}\n\n`;
}

describe("Turn 消息流空闲超时", () => {
  test("阈值必须严格大于服务端 15s 心跳间隔", () => {
    // 小于心跳间隔会把健康空闲流误判断线；该断言把口径钉死在代码里。
    expect(SSE_IDLE_TIMEOUT_MS).toBeGreaterThan(15_000);
  });

  test("连接建立后一个字节都不发：到默认阈值才响亮失败，并取消旧响应流", async () => {
    const port = 49_870;
    let streamCancelled = false;
    installStreamBackend(port, () => new Response(
      new ReadableStream<Uint8Array>({
        start() {},
        cancel() {
          streamCancelled = true;
        },
      }),
      sseInit(),
    ));
    jest.useFakeTimers();
    const pending = streamSessionMessageEvents(port, "ses_idle", "turn_idle", {});
    // 不 await 这个 promise：若超时丢失它会永不 settle，await 会让用例挂死而
    // 不是给出清晰的红色断言。改为轮询其终态，把「挂起」本身变成失败。
    // 用对象承载终态：直接断言裸变量会被 bun 的 toBeNull 断言签名收窄类型。
    const settled: { outcome: string | null; failure: Error | null } = {
      outcome: null,
      failure: null,
    };
    pending.then(
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

    // 越过阈值即响亮失败，绝不在无字节连接上无限挂起。
    jest.advanceTimersByTime(1);
    await flushMicrotasks();
    expect(settled.outcome).toBe("rejected");
    expect(settled.failure?.message).toContain("未收到任何数据");
    expect(streamCancelled).toBe(true);
  });

  test("心跳注释会重置空闲计时，而不是从建连起固定判死", async () => {
    const port = 49_871;
    const encoder = new TextEncoder();
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
    installStreamBackend(port, () => new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      }),
      sseInit(),
    ));
    jest.useFakeTimers();
    let activityCount = 0;
    const pending = streamSessionMessageEvents(port, "ses_hb", "turn_hb", {
      onActivity: () => {
        activityCount += 1;
      },
    });
    pending.catch(() => undefined);
    await flushMicrotasks();

    // 建连后 30s 内发一个心跳，再等 30s：若计时没有重置，建连满 45s 就会失败。
    jest.advanceTimersByTime(30_000);
    await flushMicrotasks();
    streamController!.enqueue(encoder.encode(": heartbeat\n\n"));
    await flushMicrotasks();
    jest.advanceTimersByTime(30_000);
    await flushMicrotasks();

    let failed = false;
    pending.catch(() => {
      failed = true;
    });
    await flushMicrotasks();
    expect(failed).toBe(false);
    expect(activityCount).toBe(1);
  });

  test("服务端按 15s 心跳保持的健康空闲流不会被误判断开", async () => {
    const port = 49_872;
    const encoder = new TextEncoder();
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
    installStreamBackend(port, () => new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          heartbeatTimer = setInterval(
            () => controller.enqueue(encoder.encode(": heartbeat\n\n")),
            15_000,
          );
        },
      }),
      sseInit(),
    ));
    jest.useFakeTimers();
    let activityCount = 0;
    const received: unknown[] = [];
    const pending = streamSessionMessageEvents(port, "ses_live", "turn_live", {
      onActivity: () => {
        activityCount += 1;
      },
      onEvent: (event) => {
        received.push(event);
      },
    });
    pending.catch(() => undefined);
    await flushMicrotasks();

    // 心跳注释不是业务事件：应当只刷新活跃时间，不产生任何事件分发。
    for (let round = 0; round < 8; round += 1) {
      jest.advanceTimersByTime(15_000);
      await flushMicrotasks();
    }
    let failed = false;
    pending.catch(() => {
      failed = true;
    });
    await flushMicrotasks();
    expect(failed).toBe(false);
    expect(activityCount).toBeGreaterThanOrEqual(8);
    expect(received).toEqual([]);
    if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
  });
});

describe("Turn 消息流 SSE 序号校验", () => {
  test("接受服务端生成的非负安全整数 id", async () => {
    const port = 49_876;
    const received: number[] = [];
    installStreamBackend(port, () => streamResponse([messageBlock("1", 1)]));

    await streamSessionMessageEvents(port, "ses_message_test", "turn_message_test", {
      onEvent: (event) => received.push(event.event_seq),
    });

    expect(received).toEqual([1]);
  });

  test("拒绝非非负安全整数的 id", async () => {
    const port = 49_873;
    installStreamBackend(port, () => streamResponse([messageBlock("not-a-seq", 1)]));

    await expect(
      streamSessionMessageEvents(port, "ses_message_test", "turn_message_test"),
    ).rejects.toThrow("SSE 消息流 id 必须是非负整数");
  });

  test("拒绝负数 id", async () => {
    const port = 49_877;
    installStreamBackend(port, () => streamResponse([messageBlock("-1", 1)]));

    await expect(
      streamSessionMessageEvents(port, "ses_message_test", "turn_message_test"),
    ).rejects.toThrow("SSE 消息流 id 必须是非负整数");
  });

  test("拒绝与 event_seq 不一致的 id", async () => {
    const port = 49_874;
    installStreamBackend(port, () => streamResponse([messageBlock("2", 1)]));

    await expect(
      streamSessionMessageEvents(port, "ses_message_test", "turn_message_test"),
    ).rejects.toThrow("SSE 消息流 id 与 event_seq 不一致");
  });

  test("拒绝同一连接内重复的 event_seq", async () => {
    const port = 49_875;
    installStreamBackend(
      port,
      () => streamResponse([messageBlock("1", 1), messageBlock("1", 1)]),
    );

    await expect(
      streamSessionMessageEvents(port, "ses_message_test", "turn_message_test"),
    ).rejects.toThrow("SSE 消息流重复 event_seq: 1");
  });
});
