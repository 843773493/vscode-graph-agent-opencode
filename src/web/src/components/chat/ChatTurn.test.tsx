import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import type { ConversationView } from "../../types/frontend";
import ChatTurn, {
  areChatTurnPropsEqual,
  type ChatTurnProps,
} from "./ChatTurn";

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
    source: "turn",
  };
}

const replayTurn = async () => {};

function chatTurnProps(value: ConversationView): ChatTurnProps {
  return {
    ...mediaProps,
    conversation: value,
    showRawDetails: false,
    isLastTurn: false,
    sessionBusy: false,
    onReplayTurn: replayTurn,
    ...pendingActionProps,
  };
}


describe("ChatTurn 轮次动作", () => {
  test("其他 Turn revision 更新时已完成 Turn 不重新渲染", () => {
    let renderCount = 0;
    const MemoProbe = React.memo(
      ({ value }: { value: ChatTurnProps }) => {
        renderCount += 1;
        return <span>{value.conversation.conversationId}</span>;
      },
      (previous, next) => areChatTurnPropsEqual(previous.value, next.value),
    );
    const first = conversation("done");
    first.turnId = "job_1";
    first.turnRevision = 4;
    first.turnItemsView = "full";
    let renderer: ReactTestRenderer;

    act(() => {
      renderer = create(<MemoProbe value={chatTurnProps(first)} />);
    });
    const reconstructed = conversation("done");
    reconstructed.turnId = "job_1";
    reconstructed.turnRevision = 4;
    reconstructed.turnItemsView = "full";
    act(() => {
      renderer!.update(<MemoProbe value={chatTurnProps(reconstructed)} />);
    });
    expect(renderCount).toBe(1);

    const revised = { ...reconstructed, turnRevision: 5 };
    act(() => {
      renderer!.update(<MemoProbe value={chatTurnProps(revised)} />);
    });
    expect(renderCount).toBe(2);
    renderer!.unmount();
  });

  test("活动 Turn 同 revision 的 streaming 更新不会被 memo 阻挡", () => {
    const previous = conversation("running", "text_delta");
    previous.turnId = "job_1";
    previous.turnRevision = 2;
    previous.turnItemsView = "full";
    const next = {
      ...previous,
      events: [
        ...previous.events,
        { ...previous.events[0], event_id: "evt_2" },
      ],
    };

    expect(areChatTurnPropsEqual(
      chatTurnProps(previous),
      chatTurnProps(next),
    )).toBe(false);
  });

  test("bounded detail 只有 text_end 时直接展示完整正文", () => {
    const value = conversation("done");
    value.assistantMessages = [];
    value.events = [{
      ...value.events[0],
      event_id: "evt_bounded_end",
      part_id: "part_bounded_end",
      type: "text_end",
      payload: { kind: "markdown", text: "Turn detail 最终正文" },
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("Turn detail 最终正文");
    expect(html).not.toContain("尚未开始");
  });

  test("用户中断显示为中性状态且不重复渲染取消事件", () => {
    const value = conversation("error", "session_interrupted");
    value.assistantMessages = [];
    value.events.push({
      ...value.events[0],
      event_id: "evt_cancelled",
      type: "job_cancelled",
    });

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("已由用户中断");
    expect(html).not.toContain("运行失败");
    expect(html.match(/已由用户中断/g)?.length).toBe(1);
  });

  test("非用户中断的取消事件使用通用取消文案", () => {
    const value = conversation("error", "job_cancelled");
    value.assistantMessages = [];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("任务已取消");
    expect(html).not.toContain("运行失败");
  });

  test("晚加入 SSE 的 startless delta 先作为活动正文展示", () => {
    const value = conversation("running");
    value.assistantMessages = [];
    value.events = [{
      ...value.events[0],
      event_id: "evt_joined_delta",
      part_id: "part_joined_delta",
      type: "text_delta",
      payload: { kind: "markdown", text: "已接入的流式片段" },
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("已接入的流式片段");
    expect(html).not.toContain("尚未开始");
  });

  test("Turn summary 先展示预览并禁止基于截断内容操作", () => {
    const value = conversation("done");
    value.turnId = "job_1";
    value.turnRevision = 1;
    value.turnItemsView = "summary";
    value.userMessage = {
      ...value.userMessage!,
      metadata: { source: "turn_projection", summary: true },
    };
    value.assistantMessages = [{
      ...value.assistantMessages![0],
      content: "最新 Turn 预览",
      metadata: { source: "turn_projection", summary: true },
    }];

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

    expect(html).toContain("最新 Turn 预览");
    expect(html).toContain("正在加载完整内容");
    expect(html).not.toContain("编辑并从此处继续");
    expect(html).not.toContain("重新生成最后回复");
  });

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

  test("内部委派任务使用专用展示且不允许编辑或重新生成", () => {
    const value = conversation("done");
    value.userMessage = {
      ...value.userMessage!,
      content: "检查认证模块",
      metadata: { internal_display_kind: "delegated_task" },
    };

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

    expect(html).toContain("委派任务");
    expect(html).toContain("检查认证模块");
    expect(html).not.toContain("编辑并从此处继续");
    expect(html).not.toContain("重新生成最后回复");
  });

  test("生成分支回报显示为内部会话生成状态", () => {
    const value = conversation("done");
    value.userMessage = {
      ...value.userMessage!,
      content: "生成分支已结束，主会话正在处理返回结果。",
      metadata: { internal_display_kind: "generated_session_result" },
    };

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

    expect(html).toContain("会话生成");
    expect(html).not.toContain("generated_session_result");
  });
});
