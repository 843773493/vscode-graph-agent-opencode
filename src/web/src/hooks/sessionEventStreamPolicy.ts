export const SESSION_STREAM_IDLE_TIMEOUT_MS = 45_000;
export const ACTIVE_JOB_RECONCILE_INTERVAL_MS = 15_000;
export const WORKSPACE_SESSION_FALLBACK_REFRESH_MS = 60_000;

const SESSION_STREAM_RECONNECT_INITIAL_MS = 1_000;
const SESSION_STREAM_RECONNECT_MAX_MS = 30_000;
const SESSION_STREAM_RECONNECT_JITTER_RATIO = 0.2;

export function sessionStreamReconnectDelay(
  attempt: number,
  randomValue: number = Math.random(),
): number {
  if (!Number.isInteger(attempt) || attempt < 0) {
    throw new Error(`事件流重连次数无效: ${attempt}`);
  }
  if (!Number.isFinite(randomValue) || randomValue < 0 || randomValue > 1) {
    throw new Error(`事件流重连随机值无效: ${randomValue}`);
  }

  const exponentialDelay = Math.min(
    SESSION_STREAM_RECONNECT_INITIAL_MS * 2 ** attempt,
    SESSION_STREAM_RECONNECT_MAX_MS,
  );
  const jitterMultiplier =
    1 - SESSION_STREAM_RECONNECT_JITTER_RATIO
    + 2 * SESSION_STREAM_RECONNECT_JITTER_RATIO * randomValue;
  return Math.round(
    Math.min(
      exponentialDelay * jitterMultiplier,
      SESSION_STREAM_RECONNECT_MAX_MS,
    ),
  );
}
