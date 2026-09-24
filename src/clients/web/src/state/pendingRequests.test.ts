import { describe, expect, test } from "bun:test";
import type { ConversationView } from "../types/frontend";

import {
  pendingSnapshotToConversations,
  sortConversationViews,
  writePendingSnapshot,
} from "./conversations";


describe("待处理消息状态", () => {
  test("以后端 enqueue_sequence 排序，并始终放在历史轮次之后", () => {
    const pending = pendingSnapshotToConversations({
      session_id: "ses_pending",
      requests: [
        {
          job_id: "job_queued",
          message_id: "msg_queued",
          session_id: "ses_pending",
          content: "后发送但排第二",
          delivery_policy: "after_turn",
          enqueue_sequence: 2,
          position: 1,
          status: "queued",
          agent_id: "default",
          message_created_at: "2026-07-17T00:00:01Z",
          created_at: "2026-07-17T00:00:01Z",
          updated_at: "2026-07-17T00:00:01Z",
          snapshot_version: 2,
        },
        {
          job_id: "job_interrupt",
          message_id: "msg_interrupt",
          session_id: "ses_pending",
          content: "消息排第一",
          delivery_policy: "after_interrupt",
          enqueue_sequence: 1,
          position: 0,
          status: "queued",
          agent_id: "default",
          message_created_at: "2026-07-17T00:00:02Z",
          created_at: "2026-07-17T00:00:02Z",
          updated_at: "2026-07-17T00:00:02Z",
          snapshot_version: 2,
        },
      ],
      snapshot_version: 2,
    });
    const history = {
      ...pending[0],
      conversationId: "msg_history",
      displayMode: "history" as const,
      pending: false,
      pendingPosition: undefined,
      source: "turn" as const,
      status: "done" as const,
    };

    expect(
      sortConversationViews([pending[0], history, pending[1]]).map(
        (conversation) => conversation.conversationId,
      ),
    ).toEqual(["msg_history", "msg_interrupt", "msg_queued"]);
  });

  test("待处理快照独立保留 active job，刷新后仍可停止或继续发送", () => {
    const pending = new Map();
    const active = new Map<string, string>();

    writePendingSnapshot(
      pending,
      active,
      {
        session_id: "ses_active",
        active_job_id: "job_active",
        requests: [],
        snapshot_version: 1,
      },
      "workspace::ses_active",
    );

    expect(active.get("workspace::ses_active")).toBe("job_active");
    expect(pending.get("workspace::ses_active")).toEqual([
      expect.objectContaining({
        jobId: "job_active",
        activeJobOverlay: true,
        status: "running",
      }),
    ]);
  });

  test("后端空快照移除终态任务时仍保留实时失败回合，直到 canonical 历史回填", () => {
    const pending = new Map<string, ConversationView[]>([
      ["workspace::ses_failed", [{
        conversationId: "msg_failed",
        displayMode: "live" as const,
        sessionId: "ses_failed",
        userMessage: {
          message_id: "msg_failed",
          session_id: "ses_failed",
          role: "user" as const,
          content: "压缩后继续",
          attachments: [],
          metadata: {},
          created_at: "2026-07-24T00:00:00Z",
          updated_at: "2026-07-24T00:00:00Z",
        },
        assistantMessages: [],
        events: [{
          event_id: "evt_failed",
          session_id: "ses_failed",
          job_id: "job_failed",
          type: "job_failed",
          phase: "job",
          title: "任务失败",
          content: "模型没有返回可见内容",
          timestamp: "2026-07-24T00:00:01Z",
        }],
        status: "error" as const,
        jobId: "job_failed",
        pending: false,
        source: "pending" as const,
      }]],
    ]);

    writePendingSnapshot(
      pending,
      new Map(),
      {
        session_id: "ses_failed",
        active_job_id: null,
        requests: [],
        snapshot_version: 2,
      },
      "workspace::ses_failed",
    );

    // 终态 SSE 可能早于 rollout 投影提交到达，此时历史详情会返回 409；空 pending
    // 快照不得提前删掉实时失败回合，必须留到下一次 bootstrap 用 canonical 历史替换。
    // 该期望与本文件之外的两条契约一致：conversations.test.ts 的「历史提交期间的
    // 终态 live Turn 不会被空 pending 快照删除」与 sessionJobReconciliation.test.ts
    // 的「失败 Job 在缺少终态 Trace 时保留可见错误回合」。
    expect(pending.get("workspace::ses_failed")).toEqual([
      expect.objectContaining({
        conversationId: "msg_failed",
        jobId: "job_failed",
        status: "error",
      }),
    ]);
  });

  test("历史刷新期间的空快照仍保留乐观 replay 回合", () => {
    const sessionId = "ses_replay_bootstrap_race";
    const mapKey = `workspace::${sessionId}`;
    const pending = new Map<string, ConversationView[]>([[mapKey, [{
      conversationId: "msg_replay_new",
      displayMode: "live",
      sessionId,
      userMessage: {
        message_id: "msg_replay_new",
        session_id: sessionId,
        role: "user",
        content: "重放上一条回复",
        attachments: [],
        metadata: {
          source: "optimistic_replay",
          replay_action: "regenerate",
        },
        created_at: "2026-07-24T00:00:00Z",
        updated_at: "2026-07-24T00:00:00Z",
      },
      assistantMessages: [],
      events: [],
      status: "running",
      jobId: "job_replay_new",
      pending: true,
      activeJobOverlay: true,
      source: "pending",
    }]]]);

    writePendingSnapshot(
      pending,
      new Map(),
      {
        session_id: sessionId,
        active_job_id: null,
        requests: [],
        snapshot_version: 1,
      },
      mapKey,
    );

    expect(pending.get(mapKey)).toEqual([
      expect.objectContaining({
        conversationId: "msg_replay_new",
        activeJobOverlay: true,
        pending: true,
      }),
    ]);
  });
});
