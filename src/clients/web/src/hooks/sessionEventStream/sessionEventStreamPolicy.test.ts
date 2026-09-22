import { describe, expect, test } from "bun:test";

import {
  ACTIVE_JOB_RECONCILE_INTERVAL_MS,
  ACTIVE_JOB_STALE_PROBE_INTERVAL_MS,
  ACTIVE_JOB_TRACE_STALE_MS,
  SESSION_STREAM_IDLE_TIMEOUT_MS,
  WORKSPACE_SESSION_FALLBACK_REFRESH_MS,
  sessionStreamReconnectDelay,
} from "./sessionEventStreamPolicy";

describe("会话事件流策略", () => {
  test("重连使用带抖动的指数退避并限制最大等待", () => {
    expect(sessionStreamReconnectDelay(0, 0.5)).toBe(1_000);
    expect(sessionStreamReconnectDelay(3, 0.5)).toBe(8_000);
    expect(sessionStreamReconnectDelay(20, 1)).toBe(30_000);
    expect(sessionStreamReconnectDelay(0, 0)).toBe(800);
    expect(sessionStreamReconnectDelay(0, 1)).toBe(1_200);
  });

  test("拒绝无效重连次数", () => {
    expect(() => sessionStreamReconnectDelay(-1, 0.5)).toThrow(
      "事件流重连次数无效",
    );
    expect(() => sessionStreamReconnectDelay(1.5, 0.5)).toThrow(
      "事件流重连次数无效",
    );
  });

  test("轮询频率保持低开销并容纳服务端心跳", () => {
    expect(ACTIVE_JOB_RECONCILE_INTERVAL_MS).toBe(5_000);
    expect(ACTIVE_JOB_TRACE_STALE_MS).toBe(8_000);
    expect(ACTIVE_JOB_STALE_PROBE_INTERVAL_MS).toBe(10_000);
    expect(SESSION_STREAM_IDLE_TIMEOUT_MS).toBe(45_000);
    expect(WORKSPACE_SESSION_FALLBACK_REFRESH_MS).toBe(60_000);
  });
});
