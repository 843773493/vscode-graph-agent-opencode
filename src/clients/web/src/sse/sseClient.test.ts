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

  test("与 Object.prototype 成员同名的事件仍按未注册处理", async () => {
    // 事件名来自网络。用普通下标读取注册表会命中 constructor / toString /
    // __proto__ 等原型成员，抛出 "definition.decode is not a function"
    // 这种掩盖根因的错误，并把未知事件伪装成已注册事件。
    for (const name of ["constructor", "toString", "valueOf", "__proto__", "hasOwnProperty"]) {
      await expect(consumeSseResponse(
        streamResponse([`event: ${name}\ndata: {}\n\n`]),
        { events: { trace: defineSseEvent(decodeJsonSseData, () => undefined) } },
      )).rejects.toThrow(`未注册的 SSE 事件类型: ${name}`);
    }
  });

  test("星号通配注册不会被 Object.prototype 成员遮蔽", async () => {
    const received: string[] = [];
    const names = ["constructor", "toString", "valueOf", "__proto__", "hasOwnProperty"];
    for (const name of names) {
      await consumeSseResponse(
        streamResponse([`event: ${name}\ndata: {}\n\n`]),
        {
          events: {
            "*": defineSseEvent(
              (_data, frame) => frame.event,
              (eventName) => received.push(eventName),
            ),
          },
        },
      );
    }
    expect(received).toEqual(names);
  });

  test("__proto__ 作为注册键只认计算属性写法，普通字面量键不构成注册", async () => {
    // 注册表是本仓库的普通对象字面量：`{ __proto__: def }` 改的是原型而不是
    // 自身键，因此不构成注册；计算属性 `{ ['__proto__']: def }` 才是自身键。
    // hasOwnProperty.call 对两种写法给出不同结论，必须与实际语义一致。
    const received: string[] = [];
    await consumeSseResponse(
      streamResponse(["event: __proto__\ndata: {}\n\n"]),
      {
        events: {
          ["__proto__"]: defineSseEvent(
            (_data, frame) => frame.event,
            (eventName: string) => received.push(eventName),
          ),
        },
      },
    );
    expect(received).toEqual(["__proto__"]);

    // 未注册时必须响亮失败，而不是退化成 Object.prototype 上被改写过的原型对象。
    await expect(consumeSseResponse(
      streamResponse(["event: __proto__\ndata: {}\n\n"]),
      { events: { trace: defineSseEvent(decodeJsonSseData, () => undefined) } },
    )).rejects.toThrow("未注册的 SSE 事件类型: __proto__");
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

  test("可在同一网络 chunk 的多帧之间让出事件循环", async () => {
    const order: string[] = [];
    await consumeSseResponse(
      streamResponse([
        "event: trace\ndata: {\"value\":1}\n\nevent: trace\ndata: {\"value\":2}\n\n",
      ]),
      {
        yieldBetweenEvents: true,
        events: {
          trace: defineSseEvent(
            decodeJsonSseData,
            (value) => {
              const numericValue = (value as { value: number }).value;
              order.push(`event-${numericValue}`);
              if (numericValue === 1) {
                queueMicrotask(() => order.push("microtask"));
              }
            },
          ),
        },
      },
    );
    expect(order).toEqual(["event-1", "microtask", "event-2"]);
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
