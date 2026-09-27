import { describe, expect, test } from "bun:test";
import type { TraceEvent } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import {
  preservePendingTerminalConversation,
  writePendingSnapshot,
} from "./pendingQueue";

const SESSION = "ses_terminal_pending_growth";

function terminalEvent(jobId: string): TraceEvent {
  return {
    event_id: `reconciled:${jobId}:job_completed`,
    session_id: SESSION,
    job_id: jobId,
    type: "job_completed",
    phase: "job",
    title: "任务完成",
    content: "",
    timestamp: "2026-09-27T00:00:00Z",
    skill_names: [],
    payload: {},
  } as TraceEvent;
}

function liveConversation(index: number): ConversationView {
  return {
    conversationId: `msg_${index}`,
    displayMode: "live",
    sessionId: SESSION,
    userMessage: {
      message_id: `msg_${index}`,
      session_id: SESSION,
      role: "user",
      content: `第 ${index} 轮`,
      attachments: [],
      metadata: { source: "optimistic", job_id: `job_${index}` },
      created_at: new Date(2026, 8, 27, 0, 0, index).toISOString(),
      updated_at: new Date(2026, 8, 27, 0, 0, index).toISOString(),
    },
    assistantMessages: [],
    events: [],
    status: "running",
    jobId: `job_${index}`,
    pending: true,
    source: "pending",
  };
}

/** 模拟「同一会话连续完成 turns 轮实时对话」：每轮都收到一帧终态 Trace，
 *  随后后端给出权威 pending 快照（此时该会话已无排队请求）。 */
function runTurns(turns: number): ConversationView[] {
  const pendingMap = new Map<string, ConversationView[]>();
  const activeJobMap = new Map<string, string>();
  for (let index = 0; index < turns; index += 1) {
    const list = pendingMap.get(SESSION) ?? [];
    pendingMap.set(SESSION, [...list, liveConversation(index)]);
    preservePendingTerminalConversation(
      pendingMap,
      SESSION,
      terminalEvent(`job_${index}`),
      "completed",
    );
    writePendingSnapshot(pendingMap, activeJobMap, {
      session_id: SESSION,
      active_job_id: null,
      requests: [],
      snapshot_version: index + 1,
    });
  }
  return pendingMap.get(SESSION) ?? [];
}

describe("pending 镜像中终态回合的保留必须是有界的", () => {
  test("同一会话连续完成多轮后，pending 列表不得随轮次无界增长", () => {
    const retained = runTurns(60).filter(
      (conversation) => conversation.source === "pending" && !conversation.pending,
    );
    expect(retained.length).toBeLessThanOrEqual(8);
  });

  test("有界裁剪必须保留最近完成的回合，供 bootstrap 前继续可见", () => {
    const retained = runTurns(60);
    expect(retained.some((conversation) => conversation.jobId === "job_59")).toBe(true);
    expect(retained.some((conversation) => conversation.jobId === "job_0")).toBe(false);
  });
});
