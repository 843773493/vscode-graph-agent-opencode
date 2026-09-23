import type { APIResponse, CursorPage } from "../types/backend";
import { JsonResponseBodyError, parseJsonResponse } from "../runtime/jsonResponseParser";
import { abortReason, awaitWithAbort } from "../utils/abortable";

export const DEFAULT_BACKEND_HOST = "127.0.0.1";
export const DEFAULT_BACKEND_PORT = 8014;
export const DEFAULT_API_REQUEST_TIMEOUT_MS = 15_000;

/**
 * 生命周期类接口的显式超时：Gateway 启动/重启本地工作区后端时会等到后端通过
 * 健康检查（app/gateway/runtime/process.py 的 GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
 * 为 120s），远端 Gateway 委托重启的上游超时为 40s。这些接口天然可能阻塞数十秒，
 * 必须显式放宽，不能被默认超时误杀成「请求超时」。
 */
export const LIFECYCLE_REQUEST_TIMEOUT_MS = 150_000;

/**
 * 工作区文件批量写操作（创建/粘贴/复制）的超时：后端在同一请求内同步落盘，
 * 大批量目录复制可能超过默认 15s，但不涉及进程生命周期，取折中上限。
 */
export const BULK_FILE_OPERATION_TIMEOUT_MS = 60_000;

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

/** 非 JSON 错误响应保留的原始文本片段上限：HTML 错误页可能极长，不能整段带进错误文案。 */
const ERROR_BODY_SNIPPET_LIMIT = 200;

/**
 * 错误响应体的唯一解析实现：优先取后端信封的 detail/message；响应体为空或不是
 * JSON 时，把这件事本身作为可诊断信息返回，而不是吞成无 detail。断流与截断响应
 * 下用户才能看到真实原因。
 */
async function readHttpErrorDetail(response: Response): Promise<unknown> {
  const raw = await response.clone().text().catch(() => null);
  if (raw === null) return "响应体不可读取";
  const trimmed = raw.trim();
  if (!trimmed) return "响应体为空";
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    const snippet = trimmed.length > ERROR_BODY_SNIPPET_LIMIT
      ? `${trimmed.slice(0, ERROR_BODY_SNIPPET_LIMIT)}…`
      : trimmed;
    return `响应体不是 JSON: ${snippet}`;
  }
  if (parsed && typeof parsed === "object") {
    const envelope = parsed as { detail?: unknown; message?: unknown };
    return envelope.detail ?? envelope.message;
  }
  return `响应体不是 JSON 对象: ${trimmed.length > ERROR_BODY_SNIPPET_LIMIT ? `${trimmed.slice(0, ERROR_BODY_SNIPPET_LIMIT)}…` : trimmed}`;
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

/**
 * 清除某个端口上缓存的本地凭据 Promise。Gateway 重启会轮换凭据，同一个 SPA
 * 进程不能永久复用旧 token；进程内按端口缓存意味着测试或同一进程的多个调用方
 * 切换后端时必须显式作废，否则会拿着上一个后端的 token 继续请求。
 */
export function invalidateGatewayToken(port: number): void {
  gatewayTokenByPort.delete(port);
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
  const path = "/api/gateway/auth/local-credential";
  // 本地凭据是工作区业务请求的前置屏障：它一旦挂起，所有等待屏障的请求都会永久
  // 卡住而没有任何可见反馈。因此这里必须与其它 JSON 请求使用同一超时上限，
  // 超时后按统一文案抛错并作废缓存，让调用方能在重试时重新获取。
  const abortState = createRequestAbortState(
    undefined,
    DEFAULT_API_REQUEST_TIMEOUT_MS,
    `请求超时: ${path}`,
  );
  const pending = fetch(`${getApiBaseUrl(port)}${path}`, {
    credentials: "include",
    signal: abortState.signal,
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
      if (abortState.didTimeout()) throw new Error(`请求超时: ${path}`);
      throw error;
    })
    .finally(() => abortState.cleanup());
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
      throw new HttpRequestError(
        response.status,
        response.statusText,
        await readHttpErrorDetail(response),
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
    timeoutMs = DEFAULT_API_REQUEST_TIMEOUT_MS,
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
        : await parseJsonResponseOrThrowDiagnostic<T>(
          response,
          path,
          parseInWorkerAboveBytes,
        ),
  );
}

/**
 * JSON 解包的唯一收口：解析失败时把正文形态与路径拼进可诊断中文错误，
 * 绝不把引擎 SyntaxError 原样透给用户。
 */
async function parseJsonResponseOrThrowDiagnostic<T>(
  response: Response,
  path: string,
  parseInWorkerAboveBytes: number | null,
): Promise<T> {
  try {
    return await parseJsonResponse<T>(response, parseInWorkerAboveBytes);
  } catch (error) {
    if (error instanceof JsonResponseBodyError) {
      throw new Error(
        jsonBodyDiagnosticMessage(response, path, error.bodyPrefix),
      );
    }
    throw error;
  }
}

export function unwrapApiData<T>(response: APIResponse<T>): T {
  validateApiEnvelope(response);
  if (response.data == null) {
    throw new Error(`后端响应缺少 data 字段: ${response.message || "unknown message"}`);
  }
  return response.data;
}

/** 响应信封的唯一校验实现：request_id 必须是非空字符串。 */
function validateApiEnvelope(response: APIResponse<unknown>): void {
  if (typeof response.request_id !== "string" || !response.request_id) {
    throw new Error("后端响应缺少 request_id");
  }
}

/**
 * 与 unwrapApiData 共用同一信封校验，但允许 data 显式为 null。
 * 适用于「空结果本身是合法业务语义」的读取接口（如用户视图状态、会话目标）；
 * data 字段缺失仍属契约被破坏，必须响亮失败。
 */
export function unwrapApiDataOrNull<T>(response: APIResponse<T | null>): T | null {
  validateApiEnvelope(response);
  if (!Object.prototype.hasOwnProperty.call(response, "data")) {
    throw new Error(`后端响应缺少 data 字段: ${response.message || "unknown message"}`);
  }
  return response.data;
}

/**
 * 2xx 成功路径收到非 JSON 响应体时的唯一诊断实现：把「代理返回了 HTML 错误页」
 * 「响应体为空」这类真实原因写进错误文案，而不是把引擎的 SyntaxError 直接暴露给用户。
 * 正文只带前缀片段，绝不把巨型载荷整段带进错误。
 */
function jsonBodyDiagnosticMessage(
  response: Response,
  path: string,
  bodyPrefix: string,
): string {
  const trimmed = bodyPrefix.trim();
  const snippet = trimmed.length > ERROR_BODY_SNIPPET_LIMIT
    ? `${trimmed.slice(0, ERROR_BODY_SNIPPET_LIMIT)}…`
    : trimmed;
  const shape = !trimmed
    ? "响应体为空"
    : trimmed.startsWith("<")
      ? "响应体看起来是 HTML"
      : "响应体不是 JSON";
  return `${path} 返回 ${response.status} ${response.statusText}，但${shape}: ${snippet}`;
}

/** 描述非法值的观测形态，供协议校验错误定位用（不打印原始内容，避免巨型载荷）。 */
function describeUnknownValue(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "数组";
  return typeof value;
}

/**
 * 校验并归一后端 CursorPage 载荷。
 *
 * 信封层（request_id、data 非空）由 unwrapApiData 负责；本函数只守载荷形状。
 * 后端 CursorPage.items 必填且保证为数组、has_more 保证为布尔
 * （app/schemas/internal_v2/common.py 的 CursorPage），因此「字段存在但类型错误」
 * 属于契约被破坏，必须响亮失败，不得伪造空页把协议损坏伪装成合法结果。
 * next_cursor 是 Optional[str]，保留其缺失/为 null 的合法语义。
 */
export function normalizePageResult<T>(value: unknown, context: string): CursorPage<T> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${context}响应必须是对象，实际为 ${describeUnknownValue(value)}`);
  }
  const record = value as { items?: unknown; next_cursor?: unknown; has_more?: unknown };
  if (!Array.isArray(record.items)) {
    throw new Error(
      `${context}响应 items 必须是数组，实际为 ${describeUnknownValue(record.items)}`,
    );
  }
  if (record.has_more !== undefined && typeof record.has_more !== "boolean") {
    throw new Error(
      `${context}响应 has_more 必须是布尔值，实际为 ${describeUnknownValue(record.has_more)}`,
    );
  }
  if (
    record.next_cursor !== undefined
    && record.next_cursor !== null
    && typeof record.next_cursor !== "string"
  ) {
    throw new Error(
      `${context}响应 next_cursor 必须是字符串或 null，实际为 ${describeUnknownValue(record.next_cursor)}`,
    );
  }
  return {
    items: record.items as T[],
    next_cursor: (record.next_cursor as string | null | undefined) ?? null,
    has_more: record.has_more as boolean | undefined,
  };
}
