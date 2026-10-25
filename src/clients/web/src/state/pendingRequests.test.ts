import { describe, expect, test } from "bun:test";
import type { ConversationView } from "../types/frontend";
import type { PendingRequestList } from "../types/backend";

import {
  pendingSnapshotToConversations,
  sortConversationViews,
  writePendingSnapshot,
} from "./conversations";

function pendingConversation(
  conversationId: string,
  jobId: string,
  options: {
    messageId?: string;
    pending?: boolean;
    turnStatus?: ConversationView["turnStatus"];
  } = {},
): ConversationView {
  const sessionId = "ses_pending_identity";
  const threadId = "thr_018f0000000070008000000000000001";
  return {
    conversationId,
    displayMode: "live",
    sessionId,
    threadId,
    userMessage: options.messageId ? {
      message_id: options.messageId,
      session_id: sessionId,
      thread_id: threadId,
      role: "user",
      content: conversationId,
      attachments: [],
      metadata: {},
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    } : null,
    assistantMessages: [],
    events: [],
    status: options.pending ? "running" : "error",
    ...(options.turnStatus === undefined ? {} : { turnStatus: options.turnStatus }),
    jobId,
    pending: options.pending ?? false,
    source: "pending",
  };
}

describe("待处理消息状态", () => {
  test("以后端 enqueue_sequence 排序，并始终放在历史轮次之后", () => {
    const snapshot: PendingRequestList = {
      session_id: "ses_pending",
      thread_id: "thr_018f0000000070008000000000000001",
      requests: [
        {
          job_id: "job_queued",
          message_id: "msg_queued",
          thread_id: "thr_018f0000000070008000000000000001",
          turn_id: "turn_queued",
          execution_binding: { job_id: "job_queued", turn_id: "turn_queued", admission_idempotency_key: "admission_queued" },
          session_id: "ses_pending",
          content: "后发送但排第二",
          delivery_policy: "after_turn",
          enqueue_sequence: 2,
          position: 1,
          status: "queued",
          agent_id: "default",
          message_created_at: "2026-07-17T00:00:01Z",
          message_metadata: { updated_at: "2026-07-17T00:05:01Z" },
          created_at: "2026-07-17T01:00:01Z",
          updated_at: "2026-07-17T02:00:01Z",
          snapshot_version: 2,
        },
        {
          job_id: "job_interrupt",
          message_id: "msg_interrupt",
          thread_id: "thr_018f0000000070008000000000000001",
          turn_id: "turn_interrupt",
          execution_binding: { job_id: "job_interrupt", turn_id: "turn_interrupt", admission_idempotency_key: "admission_interrupt" },
          session_id: "ses_pending",
          content: "消息排第一",
          delivery_policy: "after_interrupt",
          enqueue_sequence: 1,
          position: 0,
          status: "queued",
          agent_id: "default",
          message_created_at: "2026-07-17T00:00:02Z",
          message_metadata: { updated_at: "2026-07-17T00:05:02Z" },
          created_at: "2026-07-17T00:00:02Z",
          updated_at: "2026-07-17T00:00:02Z",
          snapshot_version: 2,
        },
      ],
      snapshot_version: 2,
    };
    const pending = pendingSnapshotToConversations(snapshot);
    expect(() => pendingSnapshotToConversations({ ...snapshot, thread_id: "thr_other" }))
      .toThrow("范围之外的消息");
    expect(() => pendingSnapshotToConversations({
      ...snapshot,
      requests: snapshot.requests?.map((request) => ({ ...request, message_metadata: {} })),
    })).toThrow("缺少 canonical updated_at");
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
    // Job 控制时间、冻结消息创建时间和 canonical 更新各不相同。
    expect(pending[1].userMessage?.created_at).toBe("2026-07-17T00:00:01Z");
    expect(pending[1].userMessage?.updated_at).toBe("2026-07-17T00:05:01Z");
  });

  test("主视图拒绝未声明精确 thread 的 Session 聚合快照", () => {
    expect(() => writePendingSnapshot(new Map(), new Map(), {
      session_id: "ses_pending", thread_id: null, requests: [], snapshot_version: 1,
    })).toThrow("缺少精确 thread 范围");
  });

  test("空快照按消息和 Job 两种身份去重，并保留活动 Job 与首个终态视图", () => {
    const mapKey = "workspace::ses_pending_identity";
    const pending = new Map<string, ConversationView[]>([[mapKey, [
      pendingConversation("active", "job_active", { pending: true }),
      pendingConversation("message_first", "job_message_first", {
        messageId: "msg_shared",
        turnStatus: "failed",
      }),
      pendingConversation("message_duplicate", "job_message_second", {
        messageId: "msg_shared",
        turnStatus: "failed",
      }),
      pendingConversation("job_first", "job_shared", {
        messageId: "msg_job_first",
        turnStatus: "failed",
      }),
      pendingConversation("job_duplicate", "job_shared", {
        messageId: "msg_job_second",
        turnStatus: "failed",
      }),
    ]]]);
    const activeJobs = new Map<string, string>();

    writePendingSnapshot(
      pending,
      activeJobs,
      {
        session_id: "ses_pending_identity",
        thread_id: "thr_018f0000000070008000000000000001",
        active_job_id: "job_active",
        requests: [],
        snapshot_version: 1,
      },
      mapKey,
    );

    expect(pending.get(mapKey)?.map((conversation) => conversation.conversationId)).toEqual([
      "active",
      "message_first",
      "job_first",
    ]);
    expect(activeJobs.get(mapKey)).toBe("job_active");
  });

  test("待处理快照保留活动 Job 控制状态，但没有 Turn 时不生成聊天回合", () => {
    const pending = new Map();
    const active = new Map<string, string>();

    writePendingSnapshot(
      pending,
      active,
      {
        session_id: "ses_active",
        thread_id: "thr_018f0000000070008000000000000001",
        active_job_id: "job_active",
        requests: [],
        snapshot_version: 1,
      },
      "workspace::ses_active",
    );

    expect(active.get("workspace::ses_active")).toBe("job_active");
    expect(pending.has("workspace::ses_active")).toBe(false);
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
          thread_id: "thr_018f0000000070008000000000000001",
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
          thread_id: "thr_018f0000000070008000000000000001",
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
        thread_id: "thr_018f0000000070008000000000000001",
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
        thread_id: "thr_018f0000000070008000000000000001",
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
        thread_id: "thr_018f0000000070008000000000000001",
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
