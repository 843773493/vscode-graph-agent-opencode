import { expect, test } from "bun:test";

import { applyMessageStreamProjection, messageStreamTurnIdForSession } from "./messageStreamProjection";
import type { AppState, ConversationView } from "../../types/frontend";
import type { TurnResponseItem, TurnSummary } from "../../types/backend";
import { createMessageStreamState } from "../messageStream/state";
import { createSessionTurnTimeline, upsertTurns } from "../session/turnTimeline";
import { mergeConversation } from "./conversationMerge";
import { writePendingSnapshot } from "./pendingQueue";

function acceptedStreamState(): { state: AppState; turn: TurnSummary; scope: string; thread: string } {
  const scope = "workspace::session";
  const thread = "thread";
  const turn: TurnSummary = {
    turn_id: "turn", job_id: "job", session_id: "session", status: "accepted",
    ordinal: 1, root_logical_item_ordinal: 1, source_view_id: "view", source_view_revision: 1,
    revision: 1, created_at: "2026-11-25T00:00:00Z", updated_at: "2026-11-25T00:00:00Z",
    items_view: "summary", source_message_ids: [], source_message_count: 0,
    merged_job_ids: [], merged_job_count: 0, sources_truncated: false,
    user_messages: [], user_message_count: 0, user_messages_truncated: false,
    preview_truncated: false, item_count: 0,
  };
  const state = {
    activeJobIdsBySession: new Map([[scope, turn.job_id]]),
    pendingConversations: new Map([[scope, [{
      conversationId: turn.turn_id, sessionId: turn.session_id, threadId: thread, turnId: turn.turn_id,
      jobId: turn.job_id, source: "pending", pending: true, displayMode: "live",
      status: "running", userMessage: null, events: [],
    }]]]),
    pendingQueueVersions: new Map(),
    turnTimelinesBySession: new Map([[scope, {
      ...upsertTurns(createSessionTurnTimeline(scope), [turn]), threadId: thread, phase: "ready",
    }]]),
    messageStreamsByTurnStream: new Map([["stream", {
      ...createMessageStreamState(turn.session_id, turn.turn_id, "stream", scope, thread), lastEventSeq: 1,
    }]]),
  } as unknown as AppState;
  return { state, turn, scope, thread };
}

test("空队列先退休时继续订阅 accepted Turn，消息流终态后退出且不选旧 open 镜像", () => {
  const { state, turn, scope, thread } = acceptedStreamState();
  expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBe(turn.turn_id);
  writePendingSnapshot(state.pendingConversations, state.activeJobIdsBySession, {
    session_id: turn.session_id, thread_id: thread, active_job_id: null, requests: [], snapshot_version: 1,
  }, scope, state.pendingQueueVersions);
  expect(state.pendingConversations.get(scope) ?? []).toEqual([]);
  expect(state.activeJobIdsBySession.has(scope)).toBe(false);
  expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBe(turn.turn_id);
  // 刷新历史尚未返回时，原来已确认的 Turn 不能中断尾帧订阅。
  state.turnTimelinesBySession.get(scope)!.phase = "bootstrapping";
  expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBe(turn.turn_id);
  state.messageStreamsByTurnStream!.set("terminal", {
    ...state.messageStreamsByTurnStream!.get("stream")!, streamStatus: "failed", lastEventSeq: 2,
  });
  expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBeNull();
});

test("旧回合高序号的 open 镜像不能抢走当前 Turn，当前流结束后也不重新订阅旧回合", () => {
  const { state, turn, scope, thread } = acceptedStreamState();
  state.activeJobIdsBySession.clear();
  const old = { ...state.pendingConversations.get(scope)![0]!, turnId: "old-turn", jobId: "old-job",
    pending: false, status: "error" as const, turnStatus: "failed" as const };
  state.pendingConversations.set(scope, [old]);
  state.messageStreamsByTurnStream!.set("old-stream", {
    ...createMessageStreamState(turn.session_id, old.turnId, "old-stream", scope, thread), lastEventSeq: 100,
  });
  expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBe(turn.turn_id);
  state.messageStreamsByTurnStream!.set("stream", {
    ...state.messageStreamsByTurnStream!.get("stream")!, streamStatus: "failed",
  });
  expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBeNull();
  state.turnTimelinesBySession.get(scope)!.turnsById[turn.turn_id] = { ...turn, status: "failed" };
  expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBeNull();
});

test.each(["terminal", "cached", "older_window", "older_turn", "foreign_thread", "foreign_session", "foreign_scope"])(
  "%s 不能让已退休队列恢复旧消息流订阅", (boundary) => {
    const { state, turn, scope } = acceptedStreamState();
    state.activeJobIdsBySession.clear();
    state.pendingConversations.clear();
    const timeline = state.turnTimelinesBySession.get(scope)!;
    if (boundary === "terminal") timeline.turnsById[turn.turn_id] = { ...turn, status: "failed" };
    if (boundary === "cached") timeline.restoredFromCache = true;
    if (boundary === "older_window") timeline.hasAfter = true;
    if (boundary === "older_turn") {
      timeline.orderedTurnIds.push("newer");
      timeline.turnsById.newer = { ...turn, turn_id: "newer", status: "completed" };
    }
    if (boundary === "foreign_thread") timeline.threadId = "other-thread";
    if (boundary === "foreign_session") timeline.turnsById[turn.turn_id] = { ...turn, session_id: "other-session" };
    if (boundary === "foreign_scope") timeline.scopeKey = "other-workspace::session";
    expect(messageStreamTurnIdForSession(turn.session_id, state, scope)).toBeNull();
  },
);

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
  const notice: TurnResponseItem = { id: "notice:context", kind: "context_notice", projection: "summary",
    source: { message_sequence: 0, item_id: "notice" }, context_model_call_id: "call-2" };
  const live: ConversationView = { conversationId: "turn", sessionId: "session", threadId: "thread", turnId: "turn",
    jobId: "job", displayMode: "live", status: "running", userMessage: null, assistantMessages: [], events: [],
    source: "pending", pending: true, responseItems: [notice] };
  const persisted: ConversationView = { ...live, source: "turn", responseItems: [notice,
    { kind: "text", id: "history", projection: "detail", text: "不混入实时视图", source: { message_sequence: 1 } }] };
  const merged = mergeConversation(persisted, live);
  expect(merged.responseItems).toEqual([notice]);
  const stream = createMessageStreamState("session", "turn", "stream", "scope", "thread");
  for (const terminal of [false, true]) {
    const value: ConversationView = { ...merged, ...(terminal ? { turnStatus: "completed" } : {}) };
    const streams = new Map([["stream", { ...stream, streamStatus: terminal ? "completed" as const : "open" as const }]]);
    expect(applyMessageStreamProjection([value], streams, "scope")[0].responseItems).toEqual([notice]);
  }
});
