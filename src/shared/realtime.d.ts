export interface RealtimeSubscriptionRequest {
  path: string;
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: string;
}
export class RealtimeDisconnectedError extends TypeError {}
export class RealtimeTransportError extends Error {
  constructor(status: number, message: string, requestId: string | null, context?: {
    detail?: unknown; subscriptionId?: number; path?: string; cause?: unknown;
  });
  readonly status: number;
  readonly requestId: string | null;
  readonly detail?: unknown;
  readonly subscriptionId?: number;
  readonly path?: string;
}
export function openRealtimeResponse(
  url: string,
  credential: string | Promise<string>,
  request: RealtimeSubscriptionRequest,
  signal?: AbortSignal,
  headerTimeoutMs?: number,
): Promise<Response>;
export function invalidateRealtimeConnection(url: string): void;
