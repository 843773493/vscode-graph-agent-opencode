import { expect, test } from "bun:test";

import { messageStreamTurnIdForSession } from "./messageStreamProjection";
import type { AppState } from "../../types/frontend";

test("活动 Job 命中时间线后立即停止扫描剩余 Turn", () => {
  const sessionId = "ses_stream_projection";
  const sessionCacheKey = "workspace_stream_projection::ses_stream_projection";
  const activeJobId = "job_active_stream_projection";
  const threadId = "thr_stream_projection";
  const turnsById: Record<string, { job_id: string; turn_id: string }> = {
    active: { job_id: activeJobId, turn_id: "turn_active_stream_projection" },
  };
  Object.defineProperty(turnsById, "unvisited", {
    enumerable: true,
    get: () => {
      throw new Error("命中活动 Job 后不应读取后续 Turn");
    },
  });
  const state = {
    activeJobIdsBySession: new Map([[sessionCacheKey, activeJobId]]),
    pendingConversations: new Map([[sessionCacheKey, [{ jobId: activeJobId, threadId }]]]),
    turnTimelinesBySession: new Map([[sessionCacheKey, { threadId, turnsById }]]),
  } as unknown as AppState;

  expect(messageStreamTurnIdForSession(sessionId, state, sessionCacheKey)).toBe(
    "turn_active_stream_projection",
  );
});
