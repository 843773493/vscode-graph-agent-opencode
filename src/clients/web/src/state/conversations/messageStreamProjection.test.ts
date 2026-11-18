import { expect, test } from "bun:test";

import { applyMessageStreamProjection, messageStreamTurnIdForSession } from "./messageStreamProjection";
import type { AppState, ConversationView } from "../../types/frontend";
import type { TurnResponsePart } from "../../types/backend";
import { createMessageStreamState } from "../messageStream/state";
import { mergeConversation } from "./conversationMerge";

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

test("权威上下文标记在活动与终态流保持唯一，不混入历史正文", () => {
  const notice: TurnResponsePart = { part_id: "notice:context", kind: "context_notice", projection: "summary",
    source: { message_sequence: 0, item_id: "notice" }, context_model_call_id: "call-2" };
  const live: ConversationView = { conversationId: "turn", sessionId: "session", threadId: "thread", turnId: "turn",
    jobId: "job", displayMode: "live", status: "running", userMessage: null, assistantMessages: [], events: [],
    source: "pending", pending: true, responseParts: [notice] };
  const persisted: ConversationView = { ...live, source: "turn", responseParts: [notice,
    { kind: "text", part_id: "history", projection: "detail", text: "不混入实时视图", source: { message_sequence: 1 } }] };
  const merged = mergeConversation(persisted, live);
  expect(merged.responseParts).toEqual([notice]);
  const stream = createMessageStreamState("session", "turn", "stream", "scope", "thread");
  for (const terminal of [false, true]) {
    const value: ConversationView = { ...merged, ...(terminal ? { turnStatus: "completed" } : {}) };
    const streams = new Map([["stream", { ...stream, streamStatus: terminal ? "completed" as const : "open" as const }]]);
    expect(applyMessageStreamProjection([value], streams, "scope")[0].responseParts).toEqual([notice]);
  }
});
