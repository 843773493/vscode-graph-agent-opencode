import { afterEach, describe, expect, test } from "bun:test";
import { listSessionCatalogChildren } from "./session/sessionCatalog";
import {
  DEFAULT_API_REQUEST_TIMEOUT_MS,
  getApiBaseUrl,
  getGatewayToken,
  HttpRequestError,
  invalidateGatewayToken,
  onGatewayTokenInvalidated,
  normalizePageResult,
  requestJson,
} from "./http";
import { errorDiagnostic, errorSummary } from "../utils/errorMessage";
import { restoreGlobalDescriptor } from "../tests/testGlobals";

const originalFetch = globalThis.fetch;

test("刷新失效凭据撤销原连接，释放后的监听不再接收后续失效", async () => {
  const port = 49_398;
  invalidateGatewayToken(port);
  let credentialCalls = 0;
  let invalidations = 0;
  const release = onGatewayTokenInvalidated(port, () => { invalidations += 1; });
  globalThis.fetch = Object.assign(async (input: string | URL | Request, init?: RequestInit) => {
    const path = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url).pathname;
    if (path === "/api/gateway/auth/local-credential") {
      return Response.json({ data: { token: ++credentialCalls === 1 ? "stale" : "fresh" } });
    }
    return new Headers(init?.headers).get("X-Local-Token") === "stale"
      ? Response.json({ detail: "invalid local token" }, { status: 401 })
      : Response.json({ value: "ok" });
  }, { preconnect: originalFetch.preconnect });
  try {
    await expect(requestJson(port, "/api/gateway/health")).resolves.toEqual({ value: "ok" });
    expect(credentialCalls).toBe(2);
    expect(invalidations).toBe(1);
    release();
    invalidateGatewayToken(port);
    expect(invalidations).toBe(1);
  } finally { release(); globalThis.fetch = originalFetch; invalidateGatewayToken(port); }
});

test("持续拒绝本机 token 时只刷新一次，不初始化游客或无限重试", async () => {
  const port = 49_399;
  const requests: string[] = [];
  invalidateGatewayToken(port);
  globalThis.fetch = Object.assign(async (input: string | URL | Request) => {
    const path = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url).pathname;
    requests.push(path);
    if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "rejected" } });
    return Response.json({ detail: "invalid local token" }, { status: 401 });
  }, { preconnect: originalFetch.preconnect });
  try {
    await expect(requestJson(port, "/api/v1/workspace")).rejects.toBeInstanceOf(HttpRequestError);
    expect(requests).toEqual(["/api/gateway/auth/local-credential", "/api/v1/workspace",
      "/api/gateway/auth/local-credential", "/api/v1/workspace"]);
  } finally { globalThis.fetch = originalFetch; invalidateGatewayToken(port); }
});
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

function withFetchPreconnect(
  implementation: (...args: Parameters<typeof fetch>) => ReturnType<typeof fetch>,
): typeof fetch {
  return Object.assign(implementation, { preconnect: originalFetch.preconnect });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

test("统一请求保留远端错误来源，转成文字仍能定位请求", async () => {
  const port = 49_397;
  const context = {
    source: { component: "gateway", gateway_id: "gateway-B" },
    target: { gateway_id: "gateway-B", workspace_id: "W2" },
    stage: "upstream_request", error_type: "ConnectError",
  };
  globalThis.fetch = withFetchPreconnect(async (input) => {
    if (resolveTestUrl(input, port).pathname === "/api/gateway/auth/local-credential") return tokenResponse();
    return Response.json({ detail: "上游连接失败", request_id: "req-two-hop" }, {
      status: 502,
      headers: { "X-Request-ID": "req-two-hop", "X-BoxTeam-Error-Context": JSON.stringify(context) },
    });
  });
  let failure: unknown;
  try { await requestJson(port, "/api/v1/session-catalog/children"); }
  catch (error) { failure = error; }
  expect(failure).toBeInstanceOf(HttpRequestError);
  const error = failure as HttpRequestError;
  expect(error.context).toEqual(context);
  expect(error.detail).toBe("上游连接失败");
  expect(error.requestId).toBe("req-two-hop");
  for (const fact of ["gateway-B", "W2", "upstream_request", "req-two-hop", "/api/v1/session-catalog/children"]) {
    expect(errorDiagnostic(error)).toContain(fact);
  }
});

test("错误来源响应头损坏时显式说明，仍保留原状态、正文和请求ID", () => {
  const error = new HttpRequestError(409, "Conflict", "目录版本冲突", "/catalog", "req-conflict", "{");
  expect(error.status).toBe(409);
  expect(error.detail).toBe("目录版本冲突");
  expect(error.context).toBeNull();
  expect(error.contextDiagnostic).toBe("错误来源元数据无效");
  expect(error.message).toContain("目录版本冲突");
  expect(errorDiagnostic(error)).toContain("req-conflict");
  expect(error.message).toBe("目录版本冲突");
  expect(new HttpRequestError(409, "Conflict", "目录版本冲突", "/catalog", null).contextDiagnostic).toBeUndefined();
});

test("来源组件与目标身份保留各自角色，不要求目标重复来源组件", () => {
  const context = { source: { component: "workspace_backend", backend_id: "backend-original" },
    target: { gateway_id: "gateway-route", workspace_id: "workspace-route" }, stage: "upstream_request" };
  const failure = new HttpRequestError(503, "Unavailable", "原业务失败", "/resource", "request-role", JSON.stringify(context));
  expect(failure.context).toEqual(context);
  expect(failure.contextDiagnostic).toBeUndefined();
  expect(errorSummary(failure)).toBe("原业务失败");
  expect(errorDiagnostic(failure)).toContain("source.backend_id: backend-original");
  expect(errorDiagnostic(failure)).toContain("target.gateway_id: gateway-route");
});

describe("requestJson 请求取消", () => {

  test("Gateway 重启轮换本地凭据后会刷新 token 并重试一次", async () => {
    const port = 49_300;
    installWindow(port);
    let credentialCalls = 0;
    const apiTokens: string[] = [];
    globalThis.fetch = withFetchPreconnect(
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
    );

    await expect(requestJson<{ value: string }>(port, "/api/v1/retry-after-gateway-restart", {
    }))
      .resolves.toEqual({ value: "ok" });
    expect(credentialCalls).toBe(2);
    expect(apiTokens).toEqual(["stale-token", "fresh-token"]);
  });

  test("响应体下载中超时会 abort fetch 并返回超时错误", async () => {
    const port = 49_301;
    installWindow(port);
    let requestSignal: AbortSignal | null = null;
    let markDownloadStarted: (() => void) | null = null;
    const downloadStarted = new Promise<void>((resolve) => {
      markDownloadStarted = resolve;
    });
    globalThis.fetch = withFetchPreconnect(
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
    );

    const pending = requestJson(port, "/api/v1/slow-download", {
      timeoutMs: 20,
    });
    await downloadStarted;

    const failure = await pending.catch(error => error) as HttpRequestError;
    expect(failure.message).toBe("响应正文读取超时");
    expect(failure.status).toBe(200);
    expect(failure.path).toBe("/api/v1/slow-download");
    expect((requestSignal as AbortSignal | null)?.aborted).toBe(true);
  });

  test("响应头已返回但响应体不结束时超时也会终止 JSON 解析", async () => {
    const port = 49_303;
    installWindow(port);
    let bodyController: ReadableStreamDefaultController<Uint8Array> | null = null;
    globalThis.fetch = withFetchPreconnect(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = resolveTestUrl(input, port).pathname;
        if (path === "/api/gateway/auth/local-credential") return tokenResponse();
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            bodyController = controller;
          },
        }));
      },
    );

    const pending = requestJson(port, "/api/v1/stalled-body", {
      timeoutMs: 20,
    });
    const result = await Promise.race([
      pending.then(() => "resolved", (error: unknown) => error),
      new Promise<"hung">((resolve) => globalThis.setTimeout(() => resolve("hung"), 100)),
    ]);
    (bodyController as ReadableStreamDefaultController<Uint8Array> | null)?.error(
      new Error("test cleanup"),
    );

    expect(result).not.toBe("hung");
    expect(result).toBeInstanceOf(Error);
    expect((result as Error).message).toBe("响应正文读取超时");
    expect(result).toMatchObject({ status: 200, path: "/api/v1/stalled-body" });
  });

  test("本地凭据响应体不结束时超时会终止 token 解析并清除缓存", async () => {
    const port = 49_304;
    installWindow(port);
    const originalSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = ((handler: TimerHandler, _timeout?: number, ...rest: unknown[]) =>
      (originalSetTimeout as (...args: unknown[]) => unknown)(handler, 0, ...rest)) as typeof globalThis.setTimeout;
    let bodyController: ReadableStreamDefaultController<Uint8Array> | null = null;
    globalThis.fetch = withFetchPreconnect(
      async () => new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          bodyController = controller;
        },
      }), { headers: { "X-Request-ID": "credential-body-timeout" } }),
    );

    try {
      const result = await getGatewayToken(port).then(
        () => "resolved",
        (error: unknown) => error,
      );
      (bodyController as ReadableStreamDefaultController<Uint8Array> | null)?.error(
        new Error("test cleanup"),
      );

      expect(result).toBeInstanceOf(Error);
      expect(result).toMatchObject({ status: 200, requestId: "credential-body-timeout",
        path: "/api/gateway/auth/local-credential", message: "响应正文读取超时" });
      globalThis.fetch = withFetchPreconnect(async () => tokenResponse());
      await expect(getGatewayToken(port)).resolves.toBe("http-test-token");
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }
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
    globalThis.fetch = withFetchPreconnect(
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

describe("HttpRequestError 错误体诊断", () => {
  test.each([401, 409, 200])("%s 正文中断保留原响应关联，不按 AbortError 名称视为用户取消", async status => {
    const port = 49_410 + status;
    let requests = 0;
    const context = { source: { component: "gateway", gateway_id: "gateway-original" }, stage: "http_request" };
    globalThis.fetch = withFetchPreconnect(async input => {
      if (resolveTestUrl(input, port).pathname === "/api/gateway/auth/local-credential") return tokenResponse();
      requests += 1;
      return new Response(new ReadableStream({ start(controller) {
        controller.error(new DOMException("正文连接中断 api_key=private-body-key", "AbortError"));
      } }), { status, headers: { "Content-Type": "application/json", "X-Request-ID": `body-interrupted-${status}`,
        "X-BoxTeam-Error-Context": JSON.stringify(context) } });
    });
    const failure = await requestJson(port, "/interrupted-body").catch(error => error) as HttpRequestError;
    expect(failure).toBeInstanceOf(HttpRequestError);
    expect(failure).toMatchObject({ status, requestId: `body-interrupted-${status}`, path: "/interrupted-body", context });
    expect(failure.message).toBe(status === 200 ? "响应正文无法读取" : "未提供错误原因");
    expect(errorDiagnostic(failure)).toContain("正文连接中断 api_key=[redacted]");
    expect(errorDiagnostic(failure)).not.toContain("private-body-key");
    expect(requests).toBe(1);
  });

  test.each(["<html>api_key=private-credential-key</html>", '{"data":'])("本机凭据解包失败也保留响应关联且清除失败缓存：%s", async body => {
    const port = 49_999;
    invalidateGatewayToken(port);
    let requests = 0;
    globalThis.fetch = withFetchPreconnect(async () => ++requests === 1
      ? new Response(body, { headers: { "X-Request-ID": "credential-parse-original" } }) : tokenResponse());
    const failure = await getGatewayToken(port).catch(error => error) as HttpRequestError;
    expect(failure).toBeInstanceOf(HttpRequestError);
    expect(failure).toMatchObject({ status: 200, requestId: "credential-parse-original",
      path: "/api/gateway/auth/local-credential", message: "响应无法按 JSON 解包" });
    expect(errorSummary(failure)).not.toMatch(/html|private-credential-key/);
    expect(errorDiagnostic(failure)).toContain("credential-parse-original");
    expect(errorDiagnostic(failure)).not.toContain("private-credential-key");
    await expect(getGatewayToken(port)).resolves.toBe("http-test-token");
    expect(requests).toBe(2);
  });

  test("已收到409后正文期限失败仍保留响应身份，读取故障不是业务原因", async () => {
    const port = 49_348;
    installWindow(port);
    let reads = 0;
    globalThis.fetch = withFetchPreconnect(async input => {
      if (resolveTestUrl(input, port).pathname === "/api/gateway/auth/local-credential") return tokenResponse();
      reads += 1;
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('{"detail":'));
      } }), { status: 409, headers: { "Content-Type": "application/json", "X-Request-ID": "original-conflict" } });
    });
    const failure = await requestJson(port, "/catalog", { timeoutMs: 20 }).catch(error => error) as HttpRequestError;
    expect(failure).toBeInstanceOf(HttpRequestError);
    expect(failure).toMatchObject({ status: 409, path: "/catalog", requestId: "original-conflict" });
    expect(failure.message).toBe("未提供错误原因");
    expect(failure.bodyDiagnostic).toContain("请求超时");
    expect(reads).toBe(1);
  });

  test("只有code或损坏来源头不充当原因，诊断不复制原头及任意data", () => {
    const failure = new HttpRequestError(409, "Conflict", { code: "rejected", data: { token: "private-data" } },
      "/private/path", "original-id", '{"secret":"private-header"');
    expect(errorSummary(failure)).toBe("未提供错误原因");
    const diagnostic = errorDiagnostic(failure);
    expect(diagnostic).toContain("错误来源元数据无效");
    expect(diagnostic).toContain("original-id");
    expect(diagnostic).toContain("/private/path");
    expect(diagnostic).not.toMatch(/private-header|private-data/);
  });

  function installErrorResponse(port: number, body: BodyInit | null, contentType?: string, headers: Record<string, string> = {}): void {
    globalThis.fetch = withFetchPreconnect(
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
          headers: { ...headers, ...(contentType ? { "content-type": contentType } : {}) },
        });
      },
    );
  }

  test("截断 JSON 响应体保留原始文本片段而不是吞成无 detail", async () => {
    const port = 49_341;
    installWindow(port);
    installErrorResponse(port, '{"detail": "上游中断', "application/json");

    const error = await requestJson(port, "/api/v1/broken", {
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).bodyDiagnostic).toContain("响应体不是 JSON");
    expect((error as HttpRequestError).bodyDiagnostic).toContain("上游中断");
  });

  test("HTML 错误页保留可诊断片段且截断超长正文", async () => {
    const port = 49_342;
    installWindow(port);
    const html = `<html><body>502 Bad Gateway${"x".repeat(500)}`;
    installErrorResponse(port, html, "text/html");

    const error = await requestJson(port, "/api/v1/html-error", {
    }).catch((caught: unknown) => caught);

    expect((error as HttpRequestError).bodyDiagnostic).toContain("响应体看起来是 HTML");
    expect((error as HttpRequestError).bodyDiagnostic).toContain("502 Bad Gateway");
    expect(errorSummary(error)).not.toContain("<html>");
    expect((error as Error).message.length).toBeLessThan(html.length);
  });

  test("空响应体显式报告为空，而不是退化成路径 fallback", async () => {
    const port = 49_343;
    installWindow(port);
    installErrorResponse(port, "");

    const error = await requestJson(port, "/api/v1/empty-error", {
    }).catch((caught: unknown) => caught);

    expect((error as HttpRequestError).bodyDiagnostic).toBe("响应体为空");
    expect((error as Error).message).toBe("未提供错误原因");
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
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).detail).toBe("目录读取失败");
    expect((error as Error).message).toContain("目录读取失败");
  });

  test("顶层公共错误保留 details，巨型 data 不进入展示摘要", async () => {
    const port = 49_346;
    installWindow(port);
    const detail = { code: "upstream_failed", message: "上游策略拒绝", details: { stage: "model", data: "x".repeat(100_000) } };
    installErrorResponse(port, JSON.stringify(detail), "application/json");
    const failure = await requestJson(port, "/upstream").catch(error => error) as HttpRequestError;
    expect(failure.detail).toEqual(detail);
    expect(failure.message).toContain("上游策略拒绝");
    expect(new TextEncoder().encode(failure.message).length).toBeLessThanOrEqual(8192);
  });

  test("状态文本与关联后缀不能突破展示字节预算", () => {
    const failure = new HttpRequestError(500, "状态".repeat(10_000), "原因".repeat(10_000), "/" + "路径".repeat(10_000), "request-original");
    expect(new TextEncoder().encode(failure.message).length).toBeLessThanOrEqual(8192);
    expect(failure.message).toContain("原因");
    expect(errorDiagnostic(failure)).toContain("request_id: request-original");
    expect(failure.requestId).toBe("request-original");
  });

  test("非 JSON 摘要在截断前清除 URL 凭据", async () => {
    const port = 49_347;
    installWindow(port);
    const secret = "credential".repeat(180);
    installErrorResponse(port, `连接失败 https://user:${secret}@example.invalid/path`, "text/plain");
    const failure = await requestJson(port, "/upstream").catch(error => error) as HttpRequestError;
    expect(failure.bodyDiagnostic).toContain("[redacted]@");
    expect(errorDiagnostic(failure)).not.toContain("credential");
  });

  test.each(["request-original", null])("HTTP 错误保留原 request_id，不从正文补造身份：%s", async (requestId) => {
    const port = 49_345;
    installWindow(port);
    installErrorResponse(port, JSON.stringify({ detail: "目录读取失败", request_id: requestId ?? "body-only" }),
      "application/json", requestId === null ? {} : { "X-Request-ID": requestId });
    const error = await requestJson(port, "/api/v1/session-catalog/snapshot", {
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HttpRequestError);
    const failure = error as HttpRequestError;
    expect(failure.requestId).toBe(requestId);
    expect(failure.path).toBe("/api/v1/session-catalog/snapshot");
    expect(failure.detail).toBe("目录读取失败");
    expect(failure.message).toBe("目录读取失败");
    expect(failure.message).not.toContain("body-only");
  });
});

describe("2xx 非 JSON 响应体的可诊断错误", () => {
  for (const status of [200, 500]) {
    test(`${status} 非 JSON 错误摘要不能在 200 字符上限切坏 emoji`, async () => {
      const port = 49_600 + status;
      invalidateGatewayToken(port);
      const prefix = "a".repeat(199);
      globalThis.fetch = withFetchPreconnect(async input => (
        resolveTestUrl(input, port).pathname === "/api/gateway/auth/local-credential"
          ? tokenResponse()
          : new Response(`${prefix}😀tail`, { status })
      ));
      let failure: unknown;
      try { await requestJson(port, "/api/v1/unicode-diagnostic"); }
      catch (error) { failure = error; }
      expect(failure).toBeInstanceOf(Error);
      expect((failure as HttpRequestError).bodyDiagnostic).toContain(`${prefix}😀…`);
      expect((failure as Error).message).not.toMatch(/[\uD800-\uDFFF]/u);
    });
  }

  function installSuccessResponse(
    port: number,
    body: BodyInit | null,
    status = 200,
    statusText = "OK",
    contentType?: string,
  ): void {
    globalThis.fetch = withFetchPreconnect(
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
    }).catch((caught: unknown) => caught);

    const message = errorDiagnostic(error);
    expect(message).toContain("/api/v1/workspace");
    expect(message).toContain("200");
    expect(message).toContain("响应体看起来是 HTML");
    expect(message).toContain("<!doctype html");
    expect(message).not.toContain("is not valid JSON");
    expect(errorSummary(error)).toBe("响应无法按 JSON 解包");
  });

  test("2xx 空响应体时显式报告为空而不是 EOF 解析错误", async () => {
    const port = 49_351;
    installWindow(port);
    installSuccessResponse(port, "");

    const error = await requestJson(port, "/api/v1/workspace", {
    }).catch((caught: unknown) => caught);

    const message = errorDiagnostic(error);
    expect(message).toContain("/api/v1/workspace");
    expect(message).toContain("响应体为空");
    expect(message).not.toContain("Unexpected end of JSON input");
  });

  test("2xx 纯文本时说明不是 JSON 并带出片段", async () => {
    const port = 49_352;
    installWindow(port);
    installSuccessResponse(port, "gateway is down", 200, "OK", "text/plain");

    const error = await requestJson(port, "/api/v1/workspace", {
    }).catch((caught: unknown) => caught);

    const message = errorDiagnostic(error);
    expect(message).toContain("响应体不是 JSON");
    expect(message).toContain("gateway is down");
  });

  test("2xx 合法 JSON 仍正常解包，204 仍返回 undefined", async () => {
    const port = 49_353;
    installWindow(port);
    installSuccessResponse(port, JSON.stringify({ data: { ok: 1 }, request_id: "r" }));
    await expect(
      requestJson(port, "/api/v1/workspace", { }),
    ).resolves.toEqual({ data: { ok: 1 }, request_id: "r" });

    const port204 = 49_354;
    installWindow(port204);
    installSuccessResponse(port204, null, 204, "No Content");
    await expect(
      requestJson(port204, "/api/v1/workspace", { }),
    ).resolves.toBeUndefined();
  });

  test("大正文保留多行中文与 emoji，不依赖独立 Worker 产物", async () => {
    const port = 49_356;
    installWindow(port);
    const value = "工具结果\n🧪".repeat(40_000);
    installSuccessResponse(port, JSON.stringify({ data: { value } }));

    const response = await requestJson<{ data: { value: string } }>(port, "/api/v1/large-json", {
    });

    expect(response.data.value).toBe(value);
  });

  test("正文下载完成时已取消，保留取消原因而不解析损坏正文", async () => {
    const port = 49_357;
    installWindow(port);
    const controller = new AbortController();
    const reason = new DOMException("定位已取消", "AbortError");
    globalThis.fetch = withFetchPreconnect(async (...args: Parameters<typeof fetch>) => {
      const path = resolveTestUrl(args[0], port).pathname;
      if (path === "/api/gateway/auth/local-credential") return tokenResponse();
      const response = new Response();
      response.text = async () => {
        controller.abort(reason);
        return "{broken-json";
      };
      return response;
    });

    await expect(requestJson(port, "/api/v1/aborted-json", {
      signal: controller.signal,
    })).rejects.toBe(reason);
  });

  test("超大伪造 JSON 只带出前缀片段，不把整个载荷写进错误", async () => {
    const port = 49_355;
    installWindow(port);
    const huge = "<!doctype html>" + "x".repeat(20_000);
    installSuccessResponse(port, huge, 200, "OK", "text/html");

    const error = await requestJson(port, "/api/v1/workspace", {
    }).catch((caught: unknown) => caught);

    const message = errorDiagnostic(error);
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
    globalThis.fetch = withFetchPreconnect(
      async (...args: Parameters<typeof fetch>) => {
        const path = resolveTestUrl(args[0], port).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "default-timeout-token" } });
        }
        // 永不 resolve：只有注册了超时才能收口。
        return await new Promise<Response>(() => undefined);
      },
    );
  }

  test("未显式指定 timeoutMs 的 JSON 请求也会注册默认超时", async () => {
    const port = 49_370;
    installWindow(port);
    installHangingFetch(port);

    const captured = await captureRegisteredTimeoutMs(() =>
      requestJson(port, "/api/v1/workspace", { }),
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
        timeoutMs: 60_000,
      }),
    );

    expect(captured).toBe(60_000);
  });
});
