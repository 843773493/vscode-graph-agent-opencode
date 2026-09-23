import { afterEach, describe, expect, test } from "bun:test";

import {
  HttpRequestError,
  RAW_RESPONSE_HEADER_TIMEOUT_MS,
  requestGatewayResponse,
} from "./http";
import { getSessionAttachmentBlob } from "./session/sessionMessages";
import { streamSessionActivity } from "./session/sessionActivity";
import { streamSessionMessageEvents } from "./stream/sessionMessageStream";
import { streamSessionEvents } from "./stream/sessionTraceStream";
import { streamWorkspaceFileEvents } from "./stream/workspaceFileEvents";
import { getWorkspaceRawFileBlob } from "./workspaceFilesystem";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/**
 * 把 setTimeout 的毫秒数压成 0 并运行真实 handler，使「有界」与「无界」在同一用例里
 * 立刻分叉：注册了超时的调用必须以统一中文文案收口，未注册超时的调用则永不 settle。
 */
async function runWithCompressedTimers(
  run: () => Promise<unknown>,
): Promise<{ capturedTimeoutMs: number; settled: "resolved" | "rejected" | "pending"; message: string }> {
  const originalSetTimeout = globalThis.setTimeout;
  let capturedTimeoutMs = -1;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...rest: unknown[]) => {
    if (capturedTimeoutMs < 0 && typeof timeout === "number" && timeout > 0) {
      capturedTimeoutMs = timeout;
    }
    return (originalSetTimeout as (...a: unknown[]) => unknown)(handler, 0, ...rest);
  }) as typeof globalThis.setTimeout;
  try {
    return await Promise.race([
      run().then(
        () => ({ capturedTimeoutMs, settled: "resolved" as const, message: "" }),
        (error: unknown) => ({
          capturedTimeoutMs,
          settled: "rejected" as const,
          message: error instanceof Error ? error.message : String(error),
        }),
      ),
      new Promise<{ capturedTimeoutMs: number; settled: "pending"; message: string }>((resolve) => {
        originalSetTimeout(() => resolve({ capturedTimeoutMs, settled: "pending", message: "" }), 150);
      }),
    ]);
  } finally {
    globalThis.setTimeout = originalSetTimeout;
  }
}

/** 服务端接受连接但永不回写响应头：只有注册了响应头超时才能收口。 */
function installNeverRespondingBackend(port: number): void {
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const url = new URL(String(args[0]), `http://127.0.0.1:${port}`);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({
          data: { token: `never-respond-token-${port}` },
          request_id: "probe-never-token",
        });
      }
      const signal = args[1]?.signal;
      return await new Promise<Response>((_, reject) => {
        const abort = () => reject(signal?.reason ?? new DOMException("已中止", "AbortError"));
        if (signal?.aborted) {
          abort();
          return;
        }
        signal?.addEventListener("abort", abort, { once: true });
      });
    },
    { preconnect: originalFetch.preconnect },
  );
}

/**
 * 模拟 Gateway 重启轮换本地凭据：第一次下发的 token 已失效，业务请求返回
 * 401 invalid local token，第二次下发的新 token 才有效。统一请求屏障必须据此
 * 刷新凭据并重试；任何绕过屏障的手写 fetch 都只发一次就以普通 Error 失败。
 */
function installRotatingCredentialBackend(options: {
  businessResponse: () => Response;
}): { businessRequests: string[]; credentialRequests: number } {
  let credentialRequests = 0;
  const businessRequests: string[] = [];
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const [input, init] = args;
      const url = new URL(String(input), "http://127.0.0.1");
      if (url.pathname === "/api/gateway/auth/local-credential") {
        credentialRequests += 1;
        return Response.json({
          data: { token: credentialRequests === 1 ? "stale-token" : "fresh-token" },
        });
      }
      const token = new Headers(init?.headers).get("X-Local-Token") ?? "";
      businessRequests.push(`${url.pathname}#${token}`);
      if (token === "stale-token") {
        return Response.json({ detail: "invalid local token" }, { status: 401 });
      }
      return options.businessResponse();
    },
    { preconnect: originalFetch.preconnect },
  );
  return {
    businessRequests,
    get credentialRequests() {
      return credentialRequests;
    },
  } as { businessRequests: string[]; credentialRequests: number };
}

const emptySseStream = () => new Response(": heartbeat\n\n", {
  status: 200,
  headers: { "content-type": "text/event-stream" },
});

describe("统一请求屏障 requestGatewayResponse", () => {
  test("凭据轮换后刷新 token 并重试，返回原始 Response", async () => {
    const backend = installRotatingCredentialBackend({
      businessResponse: () => new Response("raw-bytes", { status: 200 }),
    });

    const response = await requestGatewayResponse(48_601, "/api/v1/workspace/files/raw", {
      skipGatewayUserSession: true,
    });

    expect(await response.text()).toBe("raw-bytes");
    expect(backend.businessRequests).toEqual([
      "/api/v1/workspace/files/raw#stale-token",
      "/api/v1/workspace/files/raw#fresh-token",
    ]);
  });

  test("不可刷新的失败统一抛 HttpRequestError 并携带状态码", async () => {
    let credentialRequests = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const url = new URL(String(args[0]), "http://127.0.0.1");
        if (url.pathname === "/api/gateway/auth/local-credential") {
          credentialRequests += 1;
          return Response.json({ data: { token: "only-token" } });
        }
        return Response.json({ detail: "permission denied" }, { status: 403 });
      },
      { preconnect: originalFetch.preconnect },
    );

    const failure = requestGatewayResponse(48_602, "/api/v1/workspace/files/raw", {
      skipGatewayUserSession: true,
    });
    await expect(failure).rejects.toBeInstanceOf(HttpRequestError);
    await expect(failure).rejects.toMatchObject({ status: 403 });
    expect(credentialRequests).toBe(1);
  });
});

describe("二进制与流式路径共享统一凭据刷新屏障", () => {
  test("getWorkspaceRawFileBlob 凭据轮换后重试并返回二进制", async () => {
    const backend = installRotatingCredentialBackend({
      businessResponse: () => new Response(new Uint8Array([1, 2, 3]), {
        status: 200,
        headers: { "content-type": "image/png" },
      }),
    });

    const blob = await getWorkspaceRawFileBlob(48_610, "docs/a.png", "workspace-raw");

    expect(blob.size).toBe(3);
    expect(backend.businessRequests).toEqual([
      "/api/v1/workspace/files/raw#stale-token",
      "/api/v1/workspace/files/raw#fresh-token",
    ]);
  });

  test("getSessionAttachmentBlob 凭据轮换后重试并返回二进制", async () => {
    const backend = installRotatingCredentialBackend({
      businessResponse: () => new Response(new Uint8Array([9, 9]), { status: 200 }),
    });

    const blob = await getSessionAttachmentBlob(48_611, "ses_a", "file_a", "workspace-raw");

    expect(blob.size).toBe(2);
    expect(backend.businessRequests).toHaveLength(2);
  });

  test("getWorkspaceRawFileBlob 不可刷新失败时抛 HttpRequestError", async () => {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const url = new URL(String(args[0]), "http://127.0.0.1");
        if (url.pathname === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "raw-token" } });
        }
        return Response.json({ detail: "not found" }, { status: 404 });
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(getWorkspaceRawFileBlob(48_612, "missing.png", "workspace-raw"))
      .rejects.toBeInstanceOf(HttpRequestError);
  });

  test("streamSessionMessageEvents 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamSessionMessageEvents(48_620, "ses_a", "turn_a", { workspaceId: "w" });

    expect(backend.businessRequests).toEqual([
      "/api/v1/sessions/ses_a/turns/turn_a/message-stream#stale-token",
      "/api/v1/sessions/ses_a/turns/turn_a/message-stream#fresh-token",
    ]);
  });

  test("streamSessionEvents 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamSessionEvents(48_621, "ses_a", { workspaceId: "w" });

    expect(backend.businessRequests).toEqual([
      "/api/v1/sessions/ses_a/traces/stream#stale-token",
      "/api/v1/sessions/ses_a/traces/stream#fresh-token",
    ]);
  });

  test("streamSessionActivity 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamSessionActivity(48_622, "w", {});

    expect(backend.businessRequests).toEqual([
      "/api/v1/session-catalog/events/stream#stale-token",
      "/api/v1/session-catalog/events/stream#fresh-token",
    ]);
  });

  test("streamWorkspaceFileEvents 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamWorkspaceFileEvents(48_623, ["/tmp/a.ts"], { workspaceId: "w" });

    expect(backend.businessRequests).toEqual([
      "/api/v1/workspace/files/events#stale-token",
      "/api/v1/workspace/files/events#fresh-token",
    ]);
  });
});

describe("原始响应入口的响应头等待有界", () => {
  // 六个「自行消费响应体」的入口：SSE 实时流四处 + 二进制下载两处。
  const rawEntryPoints: Array<[string, (port: number) => Promise<unknown>]> = [
    ["streamWorkspaceFileEvents", (port) => streamWorkspaceFileEvents(port, ["/tmp/a.ts"], {
      workspaceId: "w",
    })],
    ["streamSessionEvents", (port) => streamSessionEvents(port, "ses_a", { workspaceId: "w" })],
    ["streamSessionActivity", (port) => streamSessionActivity(port, "w", {})],
    ["streamSessionMessageEvents", (port) =>
      streamSessionMessageEvents(port, "ses_a", "turn_a", { workspaceId: "w" })],
    ["getWorkspaceRawFileBlob", (port) => getWorkspaceRawFileBlob(port, "docs/a.png", "w")],
    ["getSessionAttachmentBlob", (port) =>
      getSessionAttachmentBlob(port, "ses_a", "file_a", "w")],
  ];

  test.each(rawEntryPoints)(
    "%s 在服务端永不回写响应头时按统一中文超时文案收口，而不是永久挂起",
    async (_label, run) => {
      const port = 48_640 + rawEntryPoints.findIndex(([label]) => label === _label);
      installNeverRespondingBackend(port);

      const result = await runWithCompressedTimers(() => run(port));

      expect(result.capturedTimeoutMs).toBe(RAW_RESPONSE_HEADER_TIMEOUT_MS);
      expect(result.settled).toBe("rejected");
      expect(result.message).toMatch(/^请求超时: \//);
    },
  );

  test("响应头一到就解除等待上限，响应体消费不受该上限约束", async () => {
    const port = 48_650;
    let requestSignal: AbortSignal | null = null;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const url = new URL(String(args[0]), `http://127.0.0.1:${port}`);
        if (url.pathname === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "header-only-token" } });
        }
        requestSignal = args[1]?.signal ?? null;
        // 响应头立即可用，但响应体在远大于上限之后才结束；健康的慢响应体不能被该上限截断。
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(": heartbeat\n\n"));
            globalThis.setTimeout(() => controller.close(), 400);
          },
        });
        return new Response(body, {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    // 显式把上限压到 30ms：若定时器不在「响应头到达」时清除，它会在下面的慢响应体
    // 消费阶段触发并 abort，本用例随之失败。这正是「只约束响应头等待」的守护点。
    const response = await requestGatewayResponse(port, "/api/v1/anything", {
      skipGatewayUserSession: true,
      timeoutMs: 30,
    });
    expect(requestSignal).not.toBeNull();

    await new Promise((resolve) => globalThis.setTimeout(resolve, 150));
    expect((requestSignal as AbortSignal | null)?.aborted).toBe(false);
    expect(await response.text()).toBe(": heartbeat\n\n");
  });
});
