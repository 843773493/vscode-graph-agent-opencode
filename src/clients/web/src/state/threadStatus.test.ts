import { expect, test } from "bun:test";
import { threadStatusSyncError, queuedThreadMessages, type ThreadStatusInput } from "./threadStatus";
import { createSessionTurnTimeline } from "./session/turnTimeline";
import { conversation } from "../components/chat/turn/chatTurnTestFixtures";
import type { ConversationView } from "../types/frontend";

function input(value: ConversationView): ThreadStatusInput {
  return { conversations: [value], timeline: { ...createSessionTurnTimeline("owner"), phase: "ready" },
    activeJobId: value.status === "running" ? value.jobId : null, active: true };
}
function running(): ConversationView {
  return { ...conversation("running"), pending: true, messageStream: { connectionStatus: "connected",
    streamStatus: "open", lastEventSeq: 8, failure: null, resumable: true,
    activeState: { kind: "model_output", phase: "reasoning", entity_id: "model", status: "running" } } };
}

test("只统计后端已接受的队列，保留后端排队顺序", () => {
  const queued: ConversationView = { ...conversation("queued"), pending: true, deliveryPolicy: "after_turn", enqueueSequence: 1, pendingPosition: 1 };
  const first = { ...queued, enqueueSequence: 2, pendingPosition: 0, conversationId: "first" };
  expect(queuedThreadMessages([queued, { ...queued, jobId: null, pendingSubmissionId: "local" },
    { ...queued, status: "error", pending: false }, { ...queued, activeJobOverlay: true }, first])).toEqual([first, queued]);
});

test("同步错误保留原文与优先级，空缓存或后台观察本身不构成错误", () => {
  const state = input(conversation("done"));
  state.timeline = { ...state.timeline!, restoredFromCache: true };
  expect(threadStatusSyncError(state)).toBeNull();
  state.timeline.error = "历史读取失败";
  expect(threadStatusSyncError(state)).toBe("历史读取失败");
  expect(threadStatusSyncError({ ...state, syncError: "HTTP 503 原始原因" })).toBe("HTTP 503 原始原因");
  expect(threadStatusSyncError({ ...input(running()), active: false })).toBeNull();
});

for (const connectionStatus of ["disconnected", "gap", "retry_exhausted"] as const) {
  test(`${connectionStatus} 诊断属于指定活动 Job，已完成轮次不再报告旧错误`, () => {
    const value = running();
    value.messageStream = { ...value.messageStream!, connectionStatus, protocolError: "原始故障原因" };
    expect(threadStatusSyncError({ ...input(value), conversations: [value, { ...conversation("done"), jobId: "older" }] })).toBe("原始故障原因");
    const complete = { ...value, status: "done" as const,
      messageStream: { ...value.messageStream, streamStatus: "completed" as const } };
    expect(threadStatusSyncError(input(complete))).toBeNull();
  });
}
