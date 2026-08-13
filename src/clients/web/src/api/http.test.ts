import { afterEach, describe, expect, test } from "bun:test";
import { requestJson } from "./http";

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

function tokenResponse(): Response {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: "request-http-token",
    data: { token: "http-test-token" },
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

describe("requestJson 请求取消", () => {
  test("响应体下载中超时会 abort fetch 并返回超时错误", async () => {
    const port = 49_301;
    installWindow(port);
    let requestSignal: AbortSignal | null = null;
    let markDownloadStarted: (() => void) | null = null;
    const downloadStarted = new Promise<void>((resolve) => {
      markDownloadStarted = resolve;
    });
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") return tokenResponse();
        requestSignal = init?.signal ?? null;
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('{"data":"'));
            requestSignal?.addEventListener("abort", () => {
              controller.error(requestSignal?.reason);
            }, { once: true });
          },
        });
        markDownloadStarted!();
        return new Response(body);
      },
      { preconnect: originalFetch.preconnect },
    );

    const pending = requestJson(port, "/api/v1/slow-download", {
      timeoutMs: 20,
      parseInWorkerAboveBytes: 1,
    });
    await downloadStarted;

    await expect(pending).rejects.toThrow("请求超时: /api/v1/slow-download");
    expect((requestSignal as AbortSignal | null)?.aborted).toBe(true);
  });

  test("外部 signal 与 timeout 组合时保留外部取消语义", async () => {
    const port = 49_302;
    installWindow(port);
    const externalController = new AbortController();
    let requestSignal: AbortSignal | null = null;
    let markFetchStarted: (() => void) | null = null;
    const fetchStarted = new Promise<void>((resolve) => {
      markFetchStarted = resolve;
    });
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") return tokenResponse();
        requestSignal = init?.signal ?? null;
        markFetchStarted!();
        return await new Promise<Response>((_, reject) => {
          const rejectAbort = () => reject(
            requestSignal?.reason ?? new DOMException("请求已取消", "AbortError"),
          );
          if (requestSignal?.aborted) {
            rejectAbort();
            return;
          }
          requestSignal?.addEventListener("abort", rejectAbort, { once: true });
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    const pending = requestJson(port, "/api/v1/external-abort", {
      timeoutMs: 1_000,
      signal: externalController.signal,
    });
    await fetchStarted;
    externalController.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(externalController.signal.aborted).toBe(true);
    expect(requestSignal).not.toBe(externalController.signal);
    expect((requestSignal as AbortSignal | null)?.aborted).toBe(true);
  });
});
