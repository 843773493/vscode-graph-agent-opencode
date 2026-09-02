import { afterEach, describe, expect, test } from "bun:test";
import { listSessionCatalogChildren } from "./sessionCatalog";
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
  test("初始化注册尚未完成时也先建立 Gateway 用户会话", async () => {
    const port = 49_302;
    installWindow(port);
    const requestedPaths: string[] = [];
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input)).pathname;
        requestedPaths.push(path);
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            data: { token: "fallback-session-token" },
            request_id: "req_fallback_token",
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "req_fallback_current",
          });
        }
        if (path === "/api/v1/workspace") {
          return Response.json({
            data: { workspace_id: "ws_fallback" },
            request_id: "req_fallback_workspace",
          });
        }
        throw new Error(`Unexpected request: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(requestJson<{ data: { workspace_id: string } }>(
      port,
      "/api/v1/workspace",
    )).resolves.toMatchObject({ data: { workspace_id: "ws_fallback" } });
    expect(requestedPaths).toEqual([
      "/api/gateway/auth/local-credential",
      "/api/gateway/users/current",
      "/api/v1/workspace",
    ]);
  });

  test("Gateway 重启轮换本地凭据后会刷新 token 并重试一次", async () => {
    const port = 49_300;
    installWindow(port);
    let credentialCalls = 0;
    const apiTokens: string[] = [];
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          credentialCalls += 1;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: `request-http-token-${credentialCalls}`,
            data: { token: credentialCalls === 1 ? "stale-token" : "fresh-token" },
          });
        }
        apiTokens.push(new Headers(init?.headers).get("X-Local-Token") ?? "");
        if (apiTokens.length === 1) {
          return Response.json({ detail: "invalid local token" }, { status: 401 });
        }
        return Response.json({ value: "ok" });
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(requestJson<{ value: string }>(port, "/api/v1/retry-after-gateway-restart", {
      skipGatewayUserSession: true,
    }))
      .resolves.toEqual({ value: "ok" });
    expect(credentialCalls).toBe(2);
    expect(apiTokens).toEqual(["stale-token", "fresh-token"]);
  });

  test("session-catalog 收到 user_session_required 时单次恢复用户会话后重试", async () => {
    const port = 49_305;
    installWindow(port);
    const requestedPaths: string[] = [];
    let currentCalls = 0;
    let guestCalls = 0;
    let catalogCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const url = new URL(String(input));
        requestedPaths.push(url.pathname);
        if (url.pathname === "/api/gateway/auth/local-credential") {
          return tokenResponse();
        }
        if (url.pathname === "/api/gateway/users/current") {
          currentCalls += 1;
          if (currentCalls === 1) {
            return Response.json({
              data: { kind: "guest", user_id: null },
              request_id: "request-current-initial",
            });
          }
          return Response.json({ detail: "user_session_required" }, { status: 401 });
        }
        if (url.pathname === "/api/gateway/users/guest") {
          guestCalls += 1;
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-guest-recovery",
          });
        }
        if (url.pathname === "/api/v1/session-catalog/children") {
          catalogCalls += 1;
          if (catalogCalls === 1) {
            return Response.json({ detail: "user_session_required" }, { status: 401 });
          }
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-catalog-recovered",
            data: {
              revision: "revision-recovered",
              parent_node_id: null,
              items: [],
              cursor: null,
              total: 0,
            },
          });
        }
        throw new Error(`Unexpected request: ${url.pathname}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(
      listSessionCatalogChildren(port, "workspace-test"),
    ).resolves.toMatchObject({ revision: "revision-recovered", items: [] });
    expect(currentCalls).toBe(2);
    expect(guestCalls).toBe(1);
    expect(catalogCalls).toBe(2);
    expect(requestedPaths).toEqual([
      "/api/gateway/auth/local-credential",
      "/api/gateway/users/current",
      "/api/v1/session-catalog/children",
      "/api/gateway/users/current",
      "/api/gateway/users/guest",
      "/api/v1/session-catalog/children",
    ]);
  });

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
      skipGatewayUserSession: true,
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
      skipGatewayUserSession: true,
    });
    await fetchStarted;
    externalController.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(externalController.signal.aborted).toBe(true);
    expect(requestSignal).not.toBe(externalController.signal);
    expect((requestSignal as AbortSignal | null)?.aborted).toBe(true);
  });
});
