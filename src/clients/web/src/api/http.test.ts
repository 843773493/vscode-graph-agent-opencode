import { afterEach, describe, expect, test } from "bun:test";
import { listSessionCatalogChildren } from "./session/sessionCatalog";
import {
  DEFAULT_API_REQUEST_TIMEOUT_MS,
  getApiBaseUrl,
  getGatewayToken,
  HttpRequestError,
  invalidateGatewayToken,
  normalizePageResult,
  requestJson,
} from "./http";
import { restoreGlobalDescriptor } from "../tests/testGlobals";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(
  port: number,
  origin = `http://127.0.0.1:${port}`,
): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port), origin },
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

function resolveTestUrl(input: RequestInfo | URL, port: number): URL {
  return new URL(String(input), `http://127.0.0.1:${port}`);
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

describe("requestJson 请求取消", () => {
  test("初始化注册尚未完成时也先建立 Gateway 用户会话", async () => {
    const port = 49_302;
    installWindow(port);
    const requestedPaths: string[] = [];
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
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
        const path = resolveTestUrl(input, port).pathname;
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
        const url = resolveTestUrl(input, port);
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
        const path = resolveTestUrl(input, port).pathname;
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
        const path = resolveTestUrl(input, port).pathname;
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

describe("HttpRequestError 错误体诊断", () => {
  function installErrorResponse(port: number, body: BodyInit | null, contentType?: string): void {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_error_token",
            data: { token: "error-test-token" },
          });
        }
        return new Response(body, {
          status: 500,
          statusText: "Internal Server Error",
          headers: contentType ? { "content-type": contentType } : undefined,
        });
      },
      { preconnect: originalFetch.preconnect },
    );
  }

  test("截断 JSON 响应体保留原始文本片段而不是吞成无 detail", async () => {
    const port = 49_341;
    installWindow(port);
    installErrorResponse(port, '{"detail": "上游中断', "application/json");

    const error = await requestJson(port, "/api/v1/broken", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as Error).message).toContain("响应体不是 JSON");
    expect((error as Error).message).toContain("上游中断");
  });

  test("HTML 错误页保留可诊断片段且截断超长正文", async () => {
    const port = 49_342;
    installWindow(port);
    const html = `<html><body>502 Bad Gateway${"x".repeat(500)}`;
    installErrorResponse(port, html, "text/html");

    const error = await requestJson(port, "/api/v1/html-error", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    expect((error as Error).message).toContain("响应体不是 JSON");
    expect((error as Error).message).toContain("502 Bad Gateway");
    expect((error as Error).message.length).toBeLessThan(html.length);
  });

  test("空响应体显式报告为空，而不是退化成路径 fallback", async () => {
    const port = 49_343;
    installWindow(port);
    installErrorResponse(port, "");

    const error = await requestJson(port, "/api/v1/empty-error", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    expect((error as Error).message).toContain("响应体为空");
  });

  test("合法 JSON 信封仍优先取 detail", async () => {
    const port = 49_344;
    installWindow(port);
    installErrorResponse(
      port,
      JSON.stringify({ detail: "目录读取失败" }),
      "application/json",
    );

    const error = await requestJson(port, "/api/v1/envelope-error", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).detail).toBe("目录读取失败");
    expect((error as Error).message).toContain("目录读取失败");
  });
});

describe("2xx 非 JSON 响应体的可诊断错误", () => {
  function installSuccessResponse(
    port: number,
    body: BodyInit | null,
    status = 200,
    statusText = "OK",
    contentType?: string,
  ): void {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_success_token",
            data: { token: "success-test-token" },
          });
        }
        return new Response(body, {
          status,
          statusText,
          headers: contentType ? { "content-type": contentType } : undefined,
        });
      },
      { preconnect: originalFetch.preconnect },
    );
  }

  test("2xx 返回 HTML 时给出中文错误并指明响应体看起来是 HTML", async () => {
    const port = 49_350;
    installWindow(port);
    installSuccessResponse(
      port,
      "<!doctype html><html><body>Gateway 未启动</body></html>",
      200,
      "OK",
      "text/html",
    );

    const error = await requestJson(port, "/api/v1/workspace", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    const message = (error as Error).message;
    expect(message).toContain("/api/v1/workspace");
    expect(message).toContain("200");
    expect(message).toContain("响应体看起来是 HTML");
    expect(message).toContain("<!doctype html");
    expect(message).not.toContain("is not valid JSON");
  });

  test("2xx 空响应体时显式报告为空而不是 EOF 解析错误", async () => {
    const port = 49_351;
    installWindow(port);
    installSuccessResponse(port, "");

    const error = await requestJson(port, "/api/v1/workspace", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    const message = (error as Error).message;
    expect(message).toContain("/api/v1/workspace");
    expect(message).toContain("响应体为空");
    expect(message).not.toContain("Unexpected end of JSON input");
  });

  test("2xx 纯文本时说明不是 JSON 并带出片段", async () => {
    const port = 49_352;
    installWindow(port);
    installSuccessResponse(port, "gateway is down", 200, "OK", "text/plain");

    const error = await requestJson(port, "/api/v1/workspace", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    const message = (error as Error).message;
    expect(message).toContain("响应体不是 JSON");
    expect(message).toContain("gateway is down");
  });

  test("2xx 合法 JSON 仍正常解包，204 仍返回 undefined", async () => {
    const port = 49_353;
    installWindow(port);
    installSuccessResponse(port, JSON.stringify({ data: { ok: 1 }, request_id: "r" }));
    await expect(
      requestJson(port, "/api/v1/workspace", { skipGatewayUserSession: true }),
    ).resolves.toEqual({ data: { ok: 1 }, request_id: "r" });

    const port204 = 49_354;
    installWindow(port204);
    installSuccessResponse(port204, null, 204, "No Content");
    await expect(
      requestJson(port204, "/api/v1/workspace", { skipGatewayUserSession: true }),
    ).resolves.toBeUndefined();
  });

  test("超大伪造 JSON 只带出前缀片段，不把整个载荷写进错误", async () => {
    const port = 49_355;
    installWindow(port);
    const huge = "<!doctype html>" + "x".repeat(20_000);
    installSuccessResponse(port, huge, 200, "OK", "text/html");

    const error = await requestJson(port, "/api/v1/workspace", {
      skipGatewayUserSession: true,
    }).catch((caught: unknown) => caught);

    const message = (error as Error).message;
    expect(message).toContain("响应体看起来是 HTML");
    expect(message.length).toBeLessThan(huge.length);
  });
});

describe("getApiBaseUrl", () => {
  test("浏览器始终使用同源相对路径，避免 localhost 与 127.0.0.1 互相跨站", () => {
    installWindow(8014, "http://localhost:8014");

    expect(getApiBaseUrl(8014)).toBe("");
  });

  test("开发前端跨端口时继续使用相对路径交给 Vite 代理", () => {
    installWindow(8011);

    expect(getApiBaseUrl(8027)).toBe("");
  });
});

describe("normalizePageResult CursorPage 契约校验", () => {
  test("合法空页正常返回", () => {
    expect(normalizePageResult<number>({ items: [], has_more: false }, "会话列表")).toEqual({
      items: [],
      next_cursor: null,
      has_more: false,
    });
  });

  test("next_cursor 为 null 时保留合法 null 语义", () => {
    expect(normalizePageResult<number>({ items: [1], next_cursor: null }, "会话列表")).toEqual({
      items: [1],
      next_cursor: null,
      has_more: undefined,
    });
  });

  test("items 非数组时响亮失败并带响应上下文", () => {
    expect(() => normalizePageResult<number>({ items: "NOT-AN-ARRAY" }, "会话列表"))
      .toThrow("会话列表响应 items 必须是数组，实际为 string");
    expect(() => normalizePageResult<number>({ items: 5 }, "会话列表"))
      .toThrow("会话列表响应 items 必须是数组，实际为 number");
    expect(() => normalizePageResult<number>({}, "会话列表"))
      .toThrow("会话列表响应 items 必须是数组，实际为 undefined");
  });

  test("载荷非对象时响亮失败", () => {
    expect(() => normalizePageResult<number>("boom", "会话列表"))
      .toThrow("会话列表响应必须是对象，实际为 string");
    expect(() => normalizePageResult<number>(null, "会话列表"))
      .toThrow("会话列表响应必须是对象，实际为 null");
    expect(() => normalizePageResult<number>([], "会话列表"))
      .toThrow("会话列表响应必须是对象，实际为 数组");
  });

  test("has_more 非布尔时响亮失败", () => {
    expect(() => normalizePageResult<number>({ items: [], has_more: "yes" }, "会话列表"))
      .toThrow("会话列表响应 has_more 必须是布尔值，实际为 string");
  });

  test("next_cursor 非字符串非 null 时响亮失败", () => {
    expect(() => normalizePageResult<number>({ items: [], next_cursor: 7 }, "会话列表"))
      .toThrow("会话列表响应 next_cursor 必须是字符串或 null，实际为 number");
  });
});

describe("Gateway 用户会话恢复循环边界", () => {
  test("连续 401 时恢复动作只执行一次，最终以 HttpRequestError 401 有界收口", async () => {
    const port = 49_360;
    installWindow(port);
    let targetCalls = 0;
    let currentCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") return tokenResponse();
        if (path === "/api/gateway/users/current") {
          currentCalls += 1;
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-current-bounded",
          });
        }
        if (path === "/api/gateway/users/guest") {
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-guest-bounded",
          });
        }
        if (path === "/api/v1/always-401") {
          targetCalls += 1;
          return Response.json({ detail: "user_session_required" }, { status: 401 });
        }
        throw new Error("Unexpected request: " + path);
      },
      { preconnect: originalFetch.preconnect },
    );

    const error = await requestJson(port, "/api/v1/always-401")
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).status).toBe(401);
    // 恢复上限为一次：目标请求最多再试一次，绝不无限重试。
    expect(targetCalls).toBe(2);
    // 初始屏障 + 401 恢复各拉取一次用户会话，证明恢复动作确实执行了一次。
    expect(currentCalls).toBe(2);
    expect((error as Error).message).not.toContain("请求未获得响应");
  });

  test("401 同时含 user_session_required 与失效 token 时两种恢复各一次后停止", async () => {
    const port = 49_361;
    installWindow(port);
    let targetCalls = 0;
    let credentialCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          credentialCalls += 1;
          return tokenResponse();
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-current-mixed",
          });
        }
        if (path === "/api/gateway/users/guest") {
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-guest-mixed",
          });
        }
        if (path === "/api/v1/mixed-401") {
          targetCalls += 1;
          return Response.json(
            { detail: "user_session_required; invalid local token" },
            { status: 401 },
          );
        }
        throw new Error("Unexpected request: " + path);
      },
      { preconnect: originalFetch.preconnect },
    );

    const error = await requestJson(port, "/api/v1/mixed-401")
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).status).toBe(401);
    // 目标请求：初始 + 会话恢复重试 + token 刷新重试，共 3 次后停止。
    expect(targetCalls).toBe(3);
    expect(credentialCalls).toBe(2);
  });

  test("恢复动作自身 500 时抛出 500 根因，绝不伪装成 401", async () => {
    const port = 49_362;
    installWindow(port);
    let currentCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") return tokenResponse();
        if (path === "/api/gateway/users/current") {
          currentCalls += 1;
          if (currentCalls === 1) {
            return Response.json({
              data: { kind: "guest", user_id: null },
              request_id: "request-current-ok",
            });
          }
          return Response.json({ detail: "用户会话服务炸了" }, { status: 500 });
        }
        if (path === "/api/v1/needs-recovery") {
          return Response.json({ detail: "user_session_required" }, { status: 401 });
        }
        throw new Error("Unexpected request: " + path);
      },
      { preconnect: originalFetch.preconnect },
    );

    const error = await requestJson(port, "/api/v1/needs-recovery")
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).status).toBe(500);
    expect((error as Error).message).toContain("用户会话服务炸了");
  });

  test("恢复期间凭据端点失败时透出可诊断根因", async () => {
    const port = 49_364;
    installWindow(port);
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return new Response("boom", { status: 500, statusText: "Internal Server Error" });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-current-cred",
          });
        }
        throw new Error("Unexpected request: " + path);
      },
      { preconnect: originalFetch.preconnect },
    );

    const error = await requestJson(port, "/api/v1/needs-credential")
      .catch((caught: unknown) => caught);

    expect((error as Error).message).toContain("获取 Gateway 本地凭据失败: HTTP 500");
  });

  test("恢复期间外部 abort 立即终止，不再发出后续目标请求", async () => {
    const port = 49_363;
    installWindow(port);
    const externalController = new AbortController();
    let targetCalls = 0;
    let currentCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") return tokenResponse();
        if (path === "/api/gateway/users/current") {
          currentCalls += 1;
          if (currentCalls === 2) externalController.abort();
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-current-abort",
          });
        }
        if (path === "/api/gateway/users/guest") {
          return Response.json({
            data: { kind: "guest", user_id: null },
            request_id: "request-guest-abort",
          });
        }
        if (path === "/api/v1/abort-during-recovery") {
          targetCalls += 1;
          return Response.json({ detail: "user_session_required" }, { status: 401 });
        }
        throw new Error("Unexpected request: " + path);
      },
      { preconnect: originalFetch.preconnect },
    );

    const error = await requestJson(port, "/api/v1/abort-during-recovery", {
      signal: externalController.signal,
    }).catch((caught: unknown) => caught);

    expect((error as Error).name).toBe("AbortError");
    // abort 后恢复循环不再发出第二次目标请求。
    expect(targetCalls).toBe(1);
  });
});

describe("默认请求超时", () => {
  /** 捕获本次请求真实注册的超时毫秒数；把定时器压成 0 避免用例真的等 15 秒。 */
  async function captureRegisteredTimeoutMs(
    run: () => Promise<unknown>,
  ): Promise<number> {
    const originalSetTimeout = globalThis.setTimeout;
    let captured = -1;
    globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...rest: unknown[]) => {
      if (captured < 0 && typeof timeout === "number" && timeout > 0) {
        captured = timeout;
      }
      return (originalSetTimeout as (...a: unknown[]) => unknown)(handler, 0, ...rest);
    }) as typeof globalThis.setTimeout;
    try {
      await run().catch(() => undefined);
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }
    return captured;
  }

  function installHangingFetch(port: number): void {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = resolveTestUrl(args[0], port).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "default-timeout-token" } });
        }
        // 永不 resolve：只有注册了超时才能收口。
        return await new Promise<Response>(() => undefined);
      },
      { preconnect: originalFetch.preconnect },
    );
  }

  test("未显式指定 timeoutMs 的 JSON 请求也会注册默认超时", async () => {
    const port = 49_370;
    installWindow(port);
    installHangingFetch(port);

    const captured = await captureRegisteredTimeoutMs(() =>
      requestJson(port, "/api/v1/workspace", { skipGatewayUserSession: true }),
    );

    expect(captured).toBe(DEFAULT_API_REQUEST_TIMEOUT_MS);
  });

  test("本地凭据获取自身也会注册默认超时并作废缓存", async () => {
    const port = 49_371;
    installWindow(port);
    installHangingFetch(port);

    const captured = await captureRegisteredTimeoutMs(() => getGatewayToken(port));

    expect(captured).toBe(DEFAULT_API_REQUEST_TIMEOUT_MS);
    // 超时后缓存必须作废，下一次调用能重新获取而不是永久复用已失败的 Promise。
    invalidateGatewayToken(port);
  });

  test("显式指定的 timeoutMs 仍然优先于默认值", async () => {
    const port = 49_372;
    installWindow(port);
    installHangingFetch(port);

    const captured = await captureRegisteredTimeoutMs(() =>
      requestJson(port, "/api/v1/workspace", {
        skipGatewayUserSession: true,
        timeoutMs: 60_000,
      }),
    );

    expect(captured).toBe(60_000);
  });
});
