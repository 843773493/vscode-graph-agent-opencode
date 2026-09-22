import type { APIResponse } from "../types/backend";
import { parseJsonResponse } from "../runtime/jsonResponseParser";

export const DEFAULT_BACKEND_HOST = "127.0.0.1";
export const DEFAULT_BACKEND_PORT = 8014;
export const DEFAULT_API_REQUEST_TIMEOUT_MS = 15_000;

interface GatewayResponseInit extends RequestInit {
  /**
   * 自行消费响应体的请求（SSE 实时流、二进制下载、multipart 上传）不建立 Gateway
   * 用户会话屏障，只共享本地凭据与刷新重试；认证初始化内部请求也置为 true。
   * 业务 JSON 请求不得绕过 Gateway 用户会话屏障。
   */
  skipGatewayUserSession?: boolean;
}

type RequestJsonInit = GatewayResponseInit & {
  timeoutMs?: number;
  parseInWorkerAboveBytes?: number;
};

export class HttpRequestError extends Error {
  constructor(
    readonly status: number,
    readonly statusText: string,
    readonly detail: unknown,
    path: string,
  ) {
    super(`请求失败 ${status} ${statusText}: ${httpErrorDetailMessage(detail, path)}`);
    this.name = "HttpRequestError";
  }
}

/**
 * 浏览器在 Gateway/前端热切换或本地服务重连的窗口内，会把尚未完成的
 * fetch 统一报告为 TypeError，而不会提供可供业务层判断的 HTTP 状态码。
 * 这类错误只能在有界重试后保留已有状态，不能把一次瞬态断连伪装成历史
 * 内容损坏。
 */
export function isTransientNetworkError(error: unknown): boolean {
  if (error instanceof HttpRequestError) return false;
  if (!error || typeof error !== "object") return false;
  const candidate = error as { name?: unknown; message?: unknown };
  const name = typeof candidate.name === "string" ? candidate.name : "";
  const message = typeof candidate.message === "string" ? candidate.message : "";
  if (name === "TimeoutError" || /请求超时/.test(message)) return false;
  return name === "AbortError"
    || name === "NetworkError"
    || /Failed to fetch|NetworkError|ERR_NETWORK_CHANGED|network changed|connection reset|连接被拒绝/i.test(
      message,
    );
}

function httpErrorDetailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = detail.message;
    if (typeof message === "string" && message.trim()) return message;
  }
  if (detail !== undefined && detail !== null) {
    const serialized = JSON.stringify(detail);
    if (serialized) return serialized;
  }
  return fallback;
}

function normalizeHeaders(headers: HeadersInit | undefined): Record<string, string> {
  if (!headers) return {};
  if (headers instanceof Headers) return Object.fromEntries(headers.entries());
  if (Array.isArray(headers)) return Object.fromEntries(headers);
  return headers;
}

async function shouldRefreshGatewayToken(response: Response): Promise<boolean> {
  if (response.status !== 401) return false;
  const body = await response.clone().json().catch(() => null) as {
    detail?: unknown;
    message?: unknown;
  } | null;
  const detail = body?.detail ?? body?.message;
  return typeof detail === "string" && detail.includes("invalid local token");
}

async function isGatewayUserSessionRequired(response: Response): Promise<boolean> {
  if (response.status !== 401) return false;
  const body = await response.clone().json().catch(() => null) as {
    detail?: unknown;
    message?: unknown;
  } | null;
  const detail = body?.detail ?? body?.message;
  if (typeof detail === "string") return detail.includes("user_session_required");
  return detail !== null
    && typeof detail === "object"
    && "code" in detail
    && detail.code === "user_session_required";
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("请求已取消", "AbortError");
}

async function awaitWithAbort<T>(
  pending: Promise<T>,
  signal: AbortSignal | undefined,
): Promise<T> {
  if (!signal) return await pending;
  if (signal.aborted) throw abortReason(signal);

  return await new Promise<T>((resolve, reject) => {
    let settled = false;
    const cleanup = () => signal.removeEventListener("abort", onAbort);
    const onAbort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(abortReason(signal));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    pending.then(
      (value) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      },
    );
  });
}

interface RequestAbortState {
  signal: AbortSignal | undefined;
  didTimeout(): boolean;
  cleanup(): void;
}

function createRequestAbortState(
  externalSignal: AbortSignal | null | undefined,
  timeoutMs: number | undefined,
  timeoutErrorMessage: string,
): RequestAbortState {
  if (!timeoutMs) {
    return {
      signal: externalSignal ?? undefined,
      didTimeout: () => false,
      cleanup: () => undefined,
    };
  }

  const controller = new AbortController();
  let timedOut = false;
  const abortFromExternal = () => controller.abort(abortReason(externalSignal!));
  if (externalSignal?.aborted) {
    abortFromExternal();
  } else {
    externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  }
  const timeoutId = globalThis.setTimeout(() => {
    if (controller.signal.aborted) return;
    timedOut = true;
    controller.abort(new DOMException(timeoutErrorMessage, "TimeoutError"));
  }, timeoutMs);

  return {
    signal: controller.signal,
    didTimeout: () => timedOut,
    cleanup: () => {
      globalThis.clearTimeout(timeoutId);
      externalSignal?.removeEventListener("abort", abortFromExternal);
    },
  };
}

export function workspaceHeader(workspaceId?: string | null): Record<string, string> {
  return workspaceId ? { "X-BoxTeam-Workspace-Id": workspaceId } : {};
}

export function getApiBaseUrl(port: number): string {
  // 浏览器中的 API 必须走当前页面同源地址：开发环境由 Vite 代理到 Gateway，
  // 发布版由 Gateway 自己托管 Web。不要在浏览器中拼接 127.0.0.1，避免
  // localhost 页面与 127.0.0.1 API 之间产生跨站本地凭据请求。
  if (typeof window !== "undefined") return "";
  return `http://${DEFAULT_BACKEND_HOST}:${port}`;
}

const gatewayTokenByPort = new Map<number, Promise<string>>();
type GatewayUserSessionInitializer = (
  port: number,
  signal?: AbortSignal,
) => Promise<unknown>;
let gatewayUserSessionInitializer: GatewayUserSessionInitializer | null = null;
const gatewayUserSessionReadyByPort = new Map<number, Promise<void>>();
const gatewayUserSessionRecoveryByPort = new Map<number, Promise<void>>();
const gatewayUserSessionWritesByPort = new Map<number, Promise<void>>();

// Gateway 用户会话 cookie 的写入必须按端口串行：acquire/takeover/游客重建
// 与 401 恢复链并发时，后到达的 Set-Cookie 会按 last-write-wins 覆盖新身份，
// 把刚完成的用户切换拉回游客态（gateway_user_view 双浏览器集成实证）。
// 条件性游客重建必须在锁内重新判定 current，不得沿用锁外的 401 结论。
export function withGatewayUserSessionWrite<T>(
  port: number,
  operation: () => Promise<T>,
): Promise<T> {
  const previous = gatewayUserSessionWritesByPort.get(port) ?? Promise.resolve();
  const result = previous.then(operation, operation);
  const guarded = result.then(() => undefined, () => undefined);
  gatewayUserSessionWritesByPort.set(port, guarded);
  void guarded.then(() => {
    if (gatewayUserSessionWritesByPort.get(port) === guarded) {
      gatewayUserSessionWritesByPort.delete(port);
    }
  });
  return result;
}

export function registerGatewayUserSessionInitializer(
  initializer: GatewayUserSessionInitializer,
): () => void {
  const previous = gatewayUserSessionInitializer;
  gatewayUserSessionInitializer = initializer;
  return () => {
    if (gatewayUserSessionInitializer === initializer) {
      gatewayUserSessionInitializer = previous;
    }
  };
}

export function invalidateGatewayUserSession(port: number): void {
  gatewayUserSessionReadyByPort.delete(port);
}

async function initializeGatewayUserSessionFallback(
  port: number,
  signal: AbortSignal | undefined,
): Promise<void> {
  await withGatewayUserSessionWrite(port, async () => {
    try {
      await requestJson<unknown>(port, "/api/gateway/users/current", {
        signal,
        skipGatewayUserSession: true,
      });
    } catch (error: unknown) {
      if (!(error instanceof HttpRequestError) || error.status !== 401) throw error;
      await requestJson<unknown>(port, "/api/gateway/users/guest", {
        method: "POST",
        body: JSON.stringify({}),
        signal,
        skipGatewayUserSession: true,
      });
    }
  });
}

async function recoverGatewayUserSession(port: number): Promise<void> {
  const existing = gatewayUserSessionRecoveryByPort.get(port);
  if (existing) return await existing;

  const recovery = (async () => {
    invalidateGatewayUserSession(port);
    // 恢复请求不复用可能已经失效的 gatewayApi pending 结果，直接完成
    // current/guest 屏障；业务请求仍会在同一 requestJson 调用中重试一次。
    await initializeGatewayUserSessionFallback(port, undefined);
  })();
  gatewayUserSessionRecoveryByPort.set(port, recovery);
  void recovery.then(
    () => {
      if (gatewayUserSessionRecoveryByPort.get(port) === recovery) {
        gatewayUserSessionRecoveryByPort.delete(port);
      }
    },
    () => {
      if (gatewayUserSessionRecoveryByPort.get(port) === recovery) {
        gatewayUserSessionRecoveryByPort.delete(port);
      }
    },
  );
  return await recovery;
}

async function ensureGatewayUserSession(
  port: number,
  signal: AbortSignal | undefined,
): Promise<void> {
  const existing = gatewayUserSessionReadyByPort.get(port);
  if (existing) {
    await awaitWithAbort(existing, signal);
    return;
  }
  const initializer = gatewayUserSessionInitializer ?? initializeGatewayUserSessionFallback;
  const initialization = initializer(port, signal).then(() => undefined);
  gatewayUserSessionReadyByPort.set(port, initialization);
  initialization.catch(() => {
    if (gatewayUserSessionReadyByPort.get(port) === initialization) {
      gatewayUserSessionReadyByPort.delete(port);
    }
  });
  await awaitWithAbort(initialization, signal);
}

export function getGatewayToken(port: number): Promise<string> {
  const existing = gatewayTokenByPort.get(port);
  if (existing) return existing;
  const pending = fetch(`${getApiBaseUrl(port)}/api/gateway/auth/local-credential`, {
    credentials: "include",
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`获取 Gateway 本地凭据失败: HTTP ${response.status}`);
      }
      const payload = await response.json() as APIResponse<{ token: string }>;
      const token = payload.data?.token;
      if (!token) throw new Error("Gateway 本地凭据响应缺少 token");
      return token;
    })
    .catch((error) => {
      gatewayTokenByPort.delete(port);
      throw error;
    });
  gatewayTokenByPort.set(port, pending);
  return pending;
}

/**
 * 带凭据请求的唯一实现：收口 Gateway 用户会话屏障（可按需跳过）、本地凭据获取与
 * 401 `invalid local token` 刷新重试、统一的 HttpRequestError。凭据与重试逻辑只
 * 存在于这里，任何入口都必须经由它，不得另写一份。
 */
async function runGatewayRequest<T>(
  port: number,
  path: string,
  init: (GatewayResponseInit & { timeoutMs?: number }) | undefined,
  consume: (response: Response) => T | Promise<T>,
): Promise<T> {
  const {
    timeoutMs,
    skipGatewayUserSession = false,
    signal,
    headers,
    ...fetchInit
  } = init ?? {};
  const timeoutErrorMessage = `请求超时: ${path}`;
  const abortState = createRequestAbortState(signal, timeoutMs, timeoutErrorMessage);

  try {
    if (!skipGatewayUserSession && path.startsWith("/api/v1/")) {
      await ensureGatewayUserSession(port, abortState.signal);
    }
    // Gateway 重启会轮换本地凭据；同一个 SPA 进程不能永久复用旧 token。
    // user_session_required 和 invalid local token 各只恢复一次，真实鉴权
    // 失败仍然向调用方抛出。
    let tokenPromise = getGatewayToken(port);
    let userSessionRecovered = false;
    let tokenRefreshed = false;
    const requestHeaders = new Headers(normalizeHeaders(headers));
    let response: Response | null = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const localToken = await awaitWithAbort(tokenPromise, abortState.signal);
      requestHeaders.set("X-Local-Token", localToken);
      response = await awaitWithAbort(
        fetch(`${getApiBaseUrl(port)}${path}`, {
          ...fetchInit,
          headers: requestHeaders,
          credentials: "include",
          signal: abortState.signal,
        }),
        abortState.signal,
      );
      if (response.status === 401) {
        if (
          !skipGatewayUserSession
          && !userSessionRecovered
          && await isGatewayUserSessionRequired(response)
        ) {
          userSessionRecovered = true;
          await recoverGatewayUserSession(port);
          continue;
        }
        if (!tokenRefreshed && await shouldRefreshGatewayToken(response)) {
          tokenRefreshed = true;
          if (gatewayTokenByPort.get(port) === tokenPromise) {
            gatewayTokenByPort.delete(port);
          }
          tokenPromise = getGatewayToken(port);
          continue;
        }
      }
      break;
    }
    if (response === null) {
      throw new Error(`请求未获得响应: ${path}`);
    }
    if (!response.ok) {
      const errorBody = await response.clone().json().catch(() => null) as {
        detail?: unknown;
        message?: string;
      } | null;
      throw new HttpRequestError(
        response.status,
        response.statusText,
        errorBody?.detail ?? errorBody?.message,
        path,
      );
    }
    return await consume(response);
  } catch (error) {
    // 超时同时可能发生在响应体消费阶段，统一按既有超时文案收口。
    if (abortState.didTimeout()) throw new Error(timeoutErrorMessage);
    throw error;
  } finally {
    abortState.cleanup();
  }
}

/**
 * 带凭据的原始 Response 唯一入口。调用方自行消费响应体，只用于 SSE 实时流、
 * 二进制下载与 multipart 上传这类不能走 JSON 解包的场景；非 2xx 已弹出
 * HttpRequestError。JSON 请求一律改用 requestJson。
 */
export async function requestGatewayResponse(
  port: number,
  path: string,
  init?: GatewayResponseInit & { timeoutMs?: number },
): Promise<Response> {
  return await runGatewayRequest(port, path, init, (response) => response);
}

export async function requestJson<T>(
  port: number,
  path: string,
  init?: RequestJsonInit,
): Promise<T> {
  const {
    timeoutMs,
    parseInWorkerAboveBytes = null,
    headers,
    ...fetchInit
  } = init ?? {};
  return await runGatewayRequest(
    port,
    path,
    {
      ...fetchInit,
      timeoutMs,
      headers: {
        ...normalizeHeaders(headers),
        ...(fetchInit.body instanceof FormData
          ? {}
          : { "Content-Type": "application/json" }),
      },
    },
    async (response) =>
      response.status === 204
        ? undefined as T
        : await parseJsonResponse<T>(response, parseInWorkerAboveBytes),
  );
}

export function unwrapApiData<T>(response: APIResponse<T>): T {
  if (typeof response.request_id !== "string" || !response.request_id) {
    throw new Error("后端响应缺少 request_id");
  }
  if (response.data == null) {
    throw new Error(`后端响应缺少 data 字段: ${response.message || "unknown message"}`);
  }
  return response.data;
}
