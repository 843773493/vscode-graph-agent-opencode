import { afterEach, describe, expect, test } from "bun:test";
import { parseJsonResponse } from "./jsonResponseParser";

const originalWorkerDescriptor = Object.getOwnPropertyDescriptor(globalThis, "Worker");

afterEach(() => {
  if (originalWorkerDescriptor) {
    Object.defineProperty(globalThis, "Worker", originalWorkerDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "Worker");
  }
});

describe("JSON response 渐进解析", () => {
  test("小响应留在当前线程解析", async () => {
    const response = Response.json({ data: { value: "small" } });

    expect(await parseJsonResponse<{ data: { value: string } }>(response, 1024))
      .toEqual({ data: { value: "small" } });
  });

  test("大响应交给 Worker 解析", async () => {
    const value = "x".repeat(300_000);
    const response = Response.json({ data: { value } });

    const parsed = await parseJsonResponse<{ data: { value: string } }>(
      response,
      256 * 1024,
    );

    expect(parsed.data.value).toBe(value);
  });

  test("损坏 JSON 透明返回 Worker 解析错误", async () => {
    const response = new Response(`{"value":"${"x".repeat(300_000)}`);

    expect(parseJsonResponse(response, 256 * 1024)).rejects.toThrow(
      "JSON Worker 解析失败",
    );
  });

  test("arrayBuffer 下载阶段取消后立即拒绝且不启动 Worker", async () => {
    let resolveBuffer: ((buffer: ArrayBuffer) => void) | null = null;
    const pendingBuffer = new Promise<ArrayBuffer>((resolve) => {
      resolveBuffer = resolve;
    });
    let workerCreated = 0;
    class UnexpectedWorker {
      constructor() {
        workerCreated += 1;
      }
    }
    Object.defineProperty(globalThis, "Worker", {
      configurable: true,
      writable: true,
      value: UnexpectedWorker,
    });
    const response = {
      arrayBuffer: () => pendingBuffer,
    } as Response;
    const controller = new AbortController();

    const parsing = parseJsonResponse(response, 1, controller.signal);
    controller.abort();

    await expect(parsing).rejects.toMatchObject({ name: "AbortError" });
    resolveBuffer!(new ArrayBuffer(1024));
    await Promise.resolve();
    expect(workerCreated).toBe(0);
  });

  test("Worker 解析阶段取消会立即 terminate 并拒绝", async () => {
    let terminateCount = 0;
    let postMessageCount = 0;
    let markWorkerStarted: (() => void) | null = null;
    const workerStarted = new Promise<void>((resolve) => {
      markWorkerStarted = resolve;
    });
    class HangingWorker {
      onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
      onerror: ((event: ErrorEvent) => void) | null = null;

      postMessage(): void {
        postMessageCount += 1;
        markWorkerStarted!();
      }

      terminate(): void {
        terminateCount += 1;
      }
    }
    Object.defineProperty(globalThis, "Worker", {
      configurable: true,
      writable: true,
      value: HangingWorker,
    });
    const response = {
      arrayBuffer: async () => new ArrayBuffer(1024),
    } as Response;
    const controller = new AbortController();

    const parsing = parseJsonResponse(response, 1, controller.signal);
    await workerStarted;
    controller.abort();

    await expect(parsing).rejects.toMatchObject({ name: "AbortError" });
    expect(postMessageCount).toBe(1);
    expect(terminateCount).toBe(1);
  });
});
