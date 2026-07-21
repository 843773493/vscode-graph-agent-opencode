import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { ConversationView } from "../../types/frontend";
import ChatTurn from "./ChatTurn";

const pendingActionProps = {
  onUpdatePending: async () => {},
  onRemovePending: async () => {},
  onSendPendingImmediately: async () => {},
  onChangePendingKind: async () => {},
};

const mediaProps = {
  apiPort: 8014,
  workspaceId: "gw_test",
};

function conversation(
  status: ConversationView["status"],
  eventType: ConversationView["events"][number]["type"] = "job_completed",
): ConversationView {
  return {
    conversationId: "msg_user",
    sessionId: "ses_web_replay",
    userMessage: {
      message_id: "msg_user",
      session_id: "ses_web_replay",
      role: "user",
      content: "原始问题",
      attachments: [],
      metadata: {},
      created_at: "2026-07-16T00:00:00Z",
      updated_at: "2026-07-16T00:00:00Z",
    },
    assistantMessages: [{
      message_id: "msg_assistant",
      session_id: "ses_web_replay",
      role: "assistant",
      content: "原始回答",
      attachments: [],
      metadata: {},
      created_at: "2026-07-16T00:00:01Z",
      updated_at: "2026-07-16T00:00:01Z",
    }],
    events: [{
      event_id: "evt_1",
      session_id: "ses_web_replay",
      job_id: "job_1",
      step_id: null,
      agent_id: "default",
      timestamp: "2026-07-16T00:00:01Z",
      type: eventType,
      payload: {},
    }],
    status,
    jobId: "job_1",
    pending: false,
    source: "messages",
  };
}


describe("ChatTurn 轮次动作", () => {
  test("最后一个完成轮次展示内联编辑和重新生成入口", () => {
    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={conversation("done")}
        showRawDetails={false}
        isLastTurn
        sessionBusy={false}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("编辑并从此处继续");
    expect(html).toContain("重新生成最后回复");
  });

  test("会话有运行中任务时禁用历史轮次编辑且不展示重新生成", () => {
    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={conversation("done")}
        showRawDetails={false}
        isLastTurn
        sessionBusy
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain('aria-label="编辑并从此处继续" disabled=""');
    expect(html).not.toContain("重新生成最后回复");
  });

  test("最后一个失败轮次展示真实重试入口", () => {
    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={conversation("error", "job_failed")}
        showRawDetails={false}
        isLastTurn
        sessionBusy={false}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("重试失败轮次");
  });

  test("同一轮后续系统 Job 的短回复不得覆盖较完整的最终答复", () => {
    const value = conversation("done");
    value.assistantMessages = [
      {
        ...value.assistantMessages![0],
        message_id: "msg_complete",
        content: "完整团队汇报：团队、成员、任务状态、子会话和审查结论均已确认。",
        metadata: { phase: "final_answer" },
      },
      {
        ...value.assistantMessages![0],
        message_id: "msg_short_notification",
        content: "已查看团队面板。",
        metadata: { phase: "final_answer" },
      },
    ];

    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={value}
        showRawDetails={false}
        isLastTurn
        sessionBusy={false}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("完整团队汇报");
    expect(html).not.toContain("已查看团队面板");
  });

  test("待处理消息展示类型、编辑、立即发送和撤回操作", () => {
    const value = conversation("queued");
    value.pending = true;
    value.pendingKind = "steering";
    value.source = "pending";
    value.assistantMessages = [];
    value.events = [];

    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={value}
        showRawDetails={false}
        isLastTurn
        sessionBusy
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("引导");
    expect(html).toContain('title="编辑"');
    expect(html).toContain('title="立即发送"');
    expect(html).toContain('title="从队列撤回"');
  });
});
