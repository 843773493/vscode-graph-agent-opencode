export interface SseFrame {
  event: string;
  id: string | null;
  data: string;
}

export interface SseEventDefinition<T = unknown> {
  decode(data: string, frame: SseFrame): T;
  handle(value: T, frame: SseFrame): void;
}

export interface ConsumeSseResponseOptions {
  events: Readonly<Record<string, SseEventDefinition>>;
  signal?: AbortSignal;
  idleTimeoutMs?: number;
  idleTimeoutError?: (timeoutMs: number) => Error;
  onActivity?: () => void;
}

export function defineSseEvent<T>(
  decode: (data: string, frame: SseFrame) => T,
  handle: (value: T, frame: SseFrame) => void,
): SseEventDefinition<T>;
export function decodeJsonSseData(data: string, frame: SseFrame): unknown;
export function parseSseFrameBlock(block: string): SseFrame | null;
export function consumeSseResponse(
  response: Response,
  options: ConsumeSseResponseOptions,
): Promise<void>;
