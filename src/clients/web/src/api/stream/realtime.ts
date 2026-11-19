import { consumeSseResponse } from "../../sse/sseClient";
import type { ConsumeSseResponseOptions } from "../../sse/sseClient";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import {
  getApiBaseUrl,
  getGatewayToken,
  invalidateGatewayToken,
  readHttpErrorDetail,
  HttpRequestError,
  onGatewayTokenInvalidated,
  RAW_RESPONSE_HEADER_TIMEOUT_MS,
} from "../http";
import {
  openRealtimeResponse,
  invalidateRealtimeConnection,
  RealtimeDisconnectedError,
  RealtimeTransportError,
} from "../../../../../shared/realtime.js";
export type { RealtimeSubscriptionRequest } from "../../../../../shared/realtime.js";
import type { RealtimeSubscriptionRequest } from "../../../../../shared/realtime.js";
export type RealtimeSubscriptionOptions = ConsumeSseResponseOptions & {
  onConnected?: (headers: Headers) => void;
};
/** 连接重试已由统一入口结清，业务层不能再叠加传输重试。 */
export class RealtimeConnectionExhaustedError extends Error {}

/** 唯一的浏览器事件订阅入口；重连重新调用 request，领域 owner 提供自己的最新游标。 */
export async function consumeRealtimeSubscription(
  port: number,
  request: () => RealtimeSubscriptionRequest,
  options: RealtimeSubscriptionOptions,
): Promise<void> {
  const url = new URL(
    "/api/gateway/realtime",
    getApiBaseUrl(port) || window.location.origin,
  );
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  const releaseToken = onGatewayTokenInvalidated(port, () =>
    invalidateRealtimeConnection(url.href),
  );
  try {
    let attempt = 0;
    let refreshedCredential = false;
    while (!options.signal?.aborted) {
      let response: Response | undefined;
      const controller = new AbortController();
      const abort = () => controller.abort(options.signal?.reason);
      options.signal?.addEventListener("abort", abort, { once: true });
      const tokenPromise = getGatewayToken(port);
      try {
        const subscription = request();
        response = await openRealtimeResponse(
          url.href,
          tokenPromise,
          subscription,
          controller.signal,
          RAW_RESPONSE_HEADER_TIMEOUT_MS,
        );
        if (!response.ok) {
          const detail = await readHttpErrorDetail(response);
          throw new HttpRequestError(
            response.status,
            response.statusText,
            detail,
            subscription.path,
            response.headers.get("X-Request-ID"),
            response.headers.get("X-BoxTeam-Error-Context"),
          );
        }
        options.onConnected?.(response.headers);
        await consumeSseResponse(response, {
          ...options,
          idleTimeoutMs: options.idleTimeoutMs ?? SSE_IDLE_TIMEOUT_MS,
          onActivity: () => {
            attempt = 0;
            options.onActivity?.();
          },
        });
        return;
      } catch (failure) {
        const error =
          failure instanceof RealtimeTransportError
            ? new HttpRequestError(
                failure.status,
                "Realtime failed",
                failure.message,
                url.pathname,
                failure.requestId,
              )
            : failure;
        if (options.signal?.aborted) return;
        if (
          !refreshedCredential &&
          error instanceof HttpRequestError &&
          error.status === 401 &&
          typeof error.detail === "string" &&
          error.detail.includes("invalid local token")
        ) {
          refreshedCredential = true;
          // 同一旧连接的所有订阅都会收到 401，只能由仍持旧凭据的一项发起刷新。
          if (getGatewayToken(port) === tokenPromise) invalidateGatewayToken(port, "refresh");
          continue;
        }
        const cause = error instanceof Error ? error.cause : undefined;
        if (
          !(error instanceof RealtimeDisconnectedError) &&
          !(cause instanceof RealtimeDisconnectedError)
        )
          throw error;
        if (attempt >= 6)
          throw new RealtimeConnectionExhaustedError(
            "Gateway 实时连接连续重连失败，已停止重试",
            { cause: error },
          );
        await new Promise<void>((resolve) => {
          const done = () => {
            clearTimeout(timer);
            options.signal?.removeEventListener("abort", done);
            resolve();
          };
          const timer = setTimeout(
            done,
            Math.min(1000 * 2 ** attempt++, 30_000),
          );
          options.signal?.addEventListener("abort", done, { once: true });
        });
      } finally {
        options.signal?.removeEventListener("abort", abort);
        controller.abort();
        if (response?.body && !response.bodyUsed) await response.body.cancel();
      }
    }
  } finally {
    releaseToken();
  }
}
