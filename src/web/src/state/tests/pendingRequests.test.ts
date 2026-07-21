import { describe, expect, test } from "bun:test";

import {
  pendingSnapshotToConversations,
  sortConversationViews,
  writePendingSnapshot,
} from "../conversations";


describe("待处理消息状态", () => {
  test("以后端 position 排序，并始终放在历史轮次之后", () => {
    const pending = pendingSnapshotToConversations({
      session_id: "ses_pending",
      requests: [
        {
          job_id: "job_queued",
          message_id: "msg_queued",
          session_id: "ses_pending",
          content: "后发送但排第二",
          kind: "queued",
          position: 1,
          agent_id: "default",
          message_created_at: "2026-07-17T00:00:01Z",
          created_at: "2026-07-17T00:00:01Z",
          updated_at: "2026-07-17T00:00:01Z",
        },
        {
          job_id: "job_steering",
          message_id: "msg_steering",
          session_id: "ses_pending",
          content: "引导消息排第一",
          kind: "steering",
          position: 0,
          agent_id: "default",
          message_created_at: "2026-07-17T00:00:02Z",
          created_at: "2026-07-17T00:00:02Z",
          updated_at: "2026-07-17T00:00:02Z",
        },
      ],
    });
    const history = {
      ...pending[0],
      conversationId: "msg_history",
      pending: false,
      pendingPosition: undefined,
      source: "messages" as const,
      status: "done" as const,
    };

    expect(
      sortConversationViews([pending[0], history, pending[1]]).map(
        (conversation) => conversation.conversationId,
      ),
    ).toEqual(["msg_history", "msg_steering", "msg_queued"]);
  });

  test("待处理快照独立保留 active job，刷新后仍可停止或发送 Steering", () => {
    const pending = new Map();
    const active = new Map<string, string>();

    writePendingSnapshot(
      pending,
      active,
      {
        session_id: "ses_active",
        active_job_id: "job_active",
        requests: [],
      },
      "workspace::ses_active",
    );

    expect(active.get("workspace::ses_active")).toBe("job_active");
    expect(pending.has("workspace::ses_active")).toBe(false);
  });
});
