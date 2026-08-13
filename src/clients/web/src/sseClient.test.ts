import { describe, expect, test } from "bun:test";

import {
  consumeSseResponse,
  decodeJsonSseData,
  defineSseEvent,
  parseSseFrameBlock,
} from "./sseClient";

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  }), { headers: { "content-type": "text/event-stream" } });
}

describe("通用 SSE 传输层", () => {
  test("解析 event、id 和多行 data", () => {
    expect(parseSseFrameBlock(
      "id: evt_1\nevent: trace\ndata: {\"a\":\ndata: 1}",
    )).toEqual({
      event: "trace",
      id: "evt_1",
      data: "{\"a\":\n1}",
    });
    expect(parseSseFrameBlock(": heartbeat")).toBeNull();
  });

  test("跨 chunk 和 CRLF 边界只交付注册事件", async () => {
    const received: unknown[] = [];
    await consumeSseResponse(
      streamResponse([
        ": heart",
        "beat\r\n\r\nevent: changes\r\ndata: {\"value\":1}\r",
        "\n\r\n",
      ]),
      {
        events: {
          changes: defineSseEvent(
            decodeJsonSseData,
            (value) => received.push(value),
          ),
        },
      },
    );
    expect(received).toEqual([{ value: 1 }]);
  });

  test("未知事件立即失败而不是静默忽略", async () => {
    await expect(consumeSseResponse(
      streamResponse(["event: unknown\ndata: {}\n\n"]),
      { events: {} },
    )).rejects.toThrow("未注册的 SSE 事件类型: unknown");
  });

  test("星号注册可以处理 Job 流的动态事件名", async () => {
    const received: string[] = [];
    const response = streamResponse(["event: job.updated\ndata: {}\n\n"]);

    await consumeSseResponse(response, {
      events: {
        "*": defineSseEvent(
          (_data, frame) => frame.event,
          (eventName) => received.push(eventName),
        ),
      },
    });

    expect(received).toEqual(["job.updated"]);
  });

  test("保留 data 字段中协议允许的首尾空格", () => {
    expect(parseSseFrameBlock("data:  value ")).toEqual({
      event: "message",
      id: null,
      data: " value ",
    });
  });

  test("支持单独 CR 分隔并拒绝错误 Content-Type", async () => {
    const received: unknown[] = [];
    await consumeSseResponse(
      streamResponse(["event: trace\rdata: {}\r\r"]),
      {
        events: {
          trace: defineSseEvent(decodeJsonSseData, (value) => received.push(value)),
        },
      },
    );
    expect(received).toEqual([{}]);

    await expect(consumeSseResponse(
      new Response("event: trace\ndata: {}\n\n", {
        headers: { "content-type": "application/json" },
      }),
      { events: {} },
    )).rejects.toThrow("SSE 响应 Content-Type 错误");
  });
});
