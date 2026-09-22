export const SESSION_STREAM_IDLE_TIMEOUT_MS = 45_000;
export const ACTIVE_JOB_RECONCILE_INTERVAL_MS = 5_000;
export const ACTIVE_JOB_TRACE_STALE_MS = 8_000;
export const ACTIVE_JOB_STALE_PROBE_INTERVAL_MS = 10_000;
export const WORKSPACE_SESSION_FALLBACK_REFRESH_MS = 60_000;

/** 连续重连次数上限：连接成功（有活动）会把计数归零，因此这里限制的是
 * 「连续若干次都没能建立连接」的退避上限，耗尽后调用方必须给出可见终态。 */
export const SESSION_STREAM_MAX_RECONNECT_ATTEMPTS = 6;

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
