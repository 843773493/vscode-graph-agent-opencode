import type { APIResponse } from "../types/backend";
import { parseJsonResponse } from "../runtime/jsonResponseParser";

export const DEFAULT_BACKEND_HOST = "127.0.0.1";
export const DEFAULT_BACKEND_PORT = 8014;

type RequestJsonInit = RequestInit & {
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

export function getGatewayToken(port: number): Promise<string> {
  const existing = gatewayTokenByPort.get(port);
  if (existing) return existing;
  const pending = fetch(`${getApiBaseUrl(port)}/api/gateway/auth/local-credential`)
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
    signal,
    headers,
    ...fetchInit
  } = init ?? {};
  const timeoutErrorMessage = `请求超时: ${path}`;
  const abortState = createRequestAbortState(signal, timeoutMs, timeoutErrorMessage);

  try {
    const localToken = await awaitWithAbort(getGatewayToken(port), abortState.signal);
    const response = await awaitWithAbort(
      fetch(`${getApiBaseUrl(port)}${path}`, {
        ...fetchInit,
        headers: {
          "Content-Type": "application/json",
          "X-Local-Token": localToken,
          ...normalizeHeaders(headers),
        },
        signal: abortState.signal,
      }),
      abortState.signal,
    );
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
