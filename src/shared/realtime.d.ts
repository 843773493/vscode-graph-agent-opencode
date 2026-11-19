export interface RealtimeSubscriptionRequest {
  path: string;
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: string;
}
export class RealtimeDisconnectedError extends TypeError {}
export class RealtimeTransportError extends Error {
  readonly status: number;
  readonly requestId: string | null;
}
export function openRealtimeResponse(
  url: string,
  credential: string | Promise<string>,
  request: RealtimeSubscriptionRequest,
  signal?: AbortSignal,
  headerTimeoutMs?: number,
): Promise<Response>;
export function invalidateRealtimeConnection(url: string): void;
