import type { APIResponse } from "../types/backend";
import { parseJsonResponse } from "../runtime/jsonResponseParser";

export const DEFAULT_BACKEND_HOST = "127.0.0.1";
export const DEFAULT_BACKEND_PORT = 8014;
export const DEFAULT_API_REQUEST_TIMEOUT_MS = 15_000;

type RequestJsonInit = RequestInit & {
  timeoutMs?: number;
  parseInWorkerAboveBytes?: number;
  /** 认证初始化内部请求使用；业务请求不应绕过 Gateway 用户会话屏障。 */
  skipGatewayUserSession?: boolean;
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
  if (typeof window !== "undefined" && window.location.port !== String(port)) return "";
  return `http://${DEFAULT_BACKEND_HOST}:${port}`;
}

const gatewayTokenByPort = new Map<number, Promise<string>>();
type GatewayUserSessionInitializer = (
  port: number,
  signal?: AbortSignal,
) => Promise<unknown>;
let gatewayUserSessionInitializer: GatewayUserSessionInitializer | null = null;
const gatewayUserSessionReadyByPort = new Map<number, Promise<void>>();

export function registerGatewayUserSessionInitializer(
  initializer: GatewayUserSessionInitializer,
): void {
  gatewayUserSessionInitializer = initializer;
}

export function invalidateGatewayUserSession(port: number): void {
  gatewayUserSessionReadyByPort.delete(port);
}

async function initializeGatewayUserSessionFallback(
  port: number,
  signal: AbortSignal | undefined,
): Promise<void> {
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

export async function requestJson<T>(
  port: number,
  path: string,
  init?: RequestJsonInit,
): Promise<T> {
  const {
    timeoutMs,
    parseInWorkerAboveBytes = null,
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
    // 401 只自动刷新一次，真实的鉴权失败仍然向调用方抛出。
    let tokenPromise = getGatewayToken(port);
    const requestHeaders = new Headers(normalizeHeaders(headers));
    if (!(fetchInit.body instanceof FormData)) {
      requestHeaders.set("Content-Type", "application/json");
    }
    let response: Response | null = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
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
      if (
        response.status !== 401
        || attempt === 1
        || !(await shouldRefreshGatewayToken(response))
      ) {
        break;
      }
      if (gatewayTokenByPort.get(port) === tokenPromise) {
        gatewayTokenByPort.delete(port);
      }
      tokenPromise = getGatewayToken(port);
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
    if (response.status === 204) return undefined as T;
    return await parseJsonResponse<T>(
      response,
      parseInWorkerAboveBytes,
      abortState.signal,
    );
  } catch (error) {
    if (abortState.didTimeout()) throw new Error(timeoutErrorMessage);
    throw error;
  } finally {
    abortState.cleanup();
  }
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
