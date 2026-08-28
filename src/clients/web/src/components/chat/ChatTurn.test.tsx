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
  onChangePendingPolicy: async () => {},
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
    displayMode: "history",
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
    responseParts: [{
      part_id: "part_final",
      kind: "final_text",
      projection: "detail",
      status: "completed",
      source: { message_sequence: 2 },
      text: "原始回答",
      final: true,
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
const loadAgentStateMessageRawContent = async () => "<system_reminder>原始消息</system_reminder>";

function chatTurnProps(value: ConversationView): ChatTurnProps {
  return {
    ...mediaProps,
    conversation: value,
    showRawDetails: false,
    isLastTurn: false,
    sessionBusy: false,
    onLoadAgentStateMessageRawContent: loadAgentStateMessageRawContent,
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
    const previous = conversation("running", "job_started");
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

  test("同 revision 的工具详情 response parts 更新不会被 memo 阻挡", () => {
    const previous = conversation("done");
    previous.turnId = "job_1";
    previous.turnRevision = 2;
    previous.turnItemsView = "full";
    const next = {
      ...previous,
      responseParts: [
        ...previous.responseParts!,
        {
          part_id: "tool-call:call-1",
          kind: "tool_call" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 1, call_index: 0 },
          tool_call_id: "call-1",
          tool_name: "inspect_fixture",
          arguments: '{"path":"fixture.json"}',
        },
      ],
    };

    expect(areChatTurnPropsEqual(
      chatTurnProps(previous),
      chatTurnProps(next),
    )).toBe(false);
  });

  test("消息流终态只有完整正文时直接展示正文", () => {
    const value = conversation("done");
  value.displayMode = "live";
  value.source = "pending";
  value.responseParts = [];
  value.messageStream = {
    connectionStatus: "terminal",
    streamStatus: "completed",
    lastEventSeq: 3,
    failure: null,
    resumable: false,
  };
  value.assistantMessages = [];
  value.responseParts = [{
    part_id: "part_bounded_end",
    kind: "text",
    projection: "streaming",
    status: "completed",
    source: { message_sequence: 0 },
    text: "Turn detail 最终正文",
    final: false,
  }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("Turn detail 最终正文");
    expect(html).not.toContain("尚未开始");
  });

  test("消息流已完成时不显示残留的 snapshot 缺口提示", () => {
    const value = conversation("done");
    value.displayMode = "live";
    value.source = "pending";
    value.messageStream = {
      connectionStatus: "gap",
      streamStatus: "completed",
      lastEventSeq: 1290,
      failure: null,
      resumable: false,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).not.toContain("实时消息流出现缺口");
  });

  test("消息流正文优先于同一视图残留的历史 response parts", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
  value.responseParts = [{
    ...value.responseParts![0],
    text: "旧历史正文",
  }];
  value.messageStream = {
    connectionStatus: "connected",
    streamStatus: "open",
    lastEventSeq: 3,
    failure: null,
    resumable: true,
  };
  value.responseParts = [{
    part_id: "live_text",
    kind: "text",
    projection: "streaming",
    status: "running",
    source: { message_sequence: 0 },
    text: "实时正文",
    final: false,
  }];
  value.assistantMessages = [];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("实时正文");
    expect(html).not.toContain("旧历史正文");
  });

  test("用户中断显示为中性状态且不重复渲染取消事件", () => {
    const value = conversation("error", "session_interrupted");
  value.displayMode = "live";
  value.source = "pending";
  value.responseParts = [];
  value.messageStream = {
    connectionStatus: "terminal",
    streamStatus: "interrupted",
    lastEventSeq: 4,
    failure: null,
    resumable: false,
  };
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
  value.displayMode = "live";
  value.source = "pending";
  value.responseParts = [];
  value.messageStream = {
    connectionStatus: "terminal",
    streamStatus: "failed",
    lastEventSeq: 3,
    failure: {
      code: "execution_cancelled",
      message: "AgentLoop 在没有用户中断请求的情况下被取消",
      afterInterruptRequested: false,
      resumable: false,
    },
    resumable: false,
  };
  value.assistantMessages = [];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("任务已取消");
    expect(html).not.toContain("运行失败");
  });

  test("消息流晚加入时已提交正文先作为活动正文展示", () => {
    const value = conversation("running");
  value.displayMode = "live";
  value.source = "pending";
  value.responseParts = [];
  value.messageStream = {
    connectionStatus: "connected",
    streamStatus: "open",
    lastEventSeq: 3,
    failure: null,
    resumable: true,
  };
  value.assistantMessages = [];
  value.responseParts = [{
    part_id: "part_joined_delta",
    kind: "text",
    projection: "streaming",
    status: "running",
    source: { message_sequence: 0 },
    text: "已接入的流式片段",
    final: false,
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
    value.responseParts = [{
      ...value.responseParts![0],
      text: "最新 Turn 预览",
      projection: "summary",
    }];

    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={value}
        showRawDetails={false}
        isLastTurn
        sessionBusy={false}
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("最新 Turn 预览");
    expect(html).not.toContain("已完成思考");
    expect(html).toContain("展开 Turn 中间消息");
    expect(html).not.toContain("编辑并从此处继续");
    expect(html).not.toContain("重新生成最后回复");
  });

  test("历史 Turn 的统计行点击后通过详情接口加载中间消息", async () => {
    const value = conversation("done");
    value.turnId = "job_1";
    value.turnRevision = 1;
    value.turnItemsView = "summary";
    value.activityStats = {
      duration_ms: 1250,
      message_count: 5,
    };
    let loadedInclude: string[] | undefined;
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChatTurn
          {...chatTurnProps(value)}
          onLoadTurnDetails={async (_turnIds, _identity, _refresh, include) => {
            loadedInclude = include;
          }}
        />,
      );
    });
    const toggle = renderer!.root.findByProps({
      "aria-label": "展开 Turn 中间消息",
    });
    await act(async () => {
      await toggle.props.onClick();
    });
    expect(loadedInclude).toEqual([
      "user",
      "text",
      "reasoning_detail",
      "encrypted_reasoning_meta",
      "tool_summary",
      "tool_call",
      "tool_result",
      "final_response",
    ]);
    expect(renderer!.root.findByProps({ "aria-expanded": true })).toBeTruthy();
    const summaryHtml = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
    expect(summaryHtml).toContain("耗时 1.3s · 消息 5 条");
    expect(summaryHtml).not.toContain("assistant 1");
    expect(summaryHtml).not.toContain("tool 1/1");
    renderer!.unmount();
  });

  test("历史 response parts 展开详情时不重复渲染工具和最终响应", async () => {
    const value = conversation("done");
    value.turnId = "job_1";
    value.turnRevision = 1;
    value.turnItemsView = "full";
    value.responseParts = [
      {
        part_id: "tool-call-1",
        kind: "tool_call",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 2,
          assistant_message_sequence: 2,
          call_index: 0,
        },
        tool_call_id: "call-1",
        tool_name: "read_fixture",
        arguments: '{"path":"fixture.json"}',
      },
      {
        part_id: "tool-result-1",
        kind: "tool_result",
        projection: "detail",
        status: "completed",
        source: {
          message_sequence: 3,
          assistant_message_sequence: 2,
          call_index: 0,
          result_message_sequence: 3,
        },
        tool_call_id: "call-1",
        tool_name: "read_fixture",
        text: "工具输出",
        result: "工具输出",
      },
      {
        ...value.responseParts![0],
        part_id: "final-1",
        text: "最终响应",
        final: true,
      },
    ];
    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(
        <ChatTurn
          {...chatTurnProps(value)}
          onLoadTurnDetails={async () => {}}
        />,
      );
    });

    const toggle = renderer!.root.findByProps({
      "aria-label": "展开 Turn 中间消息",
    });
    await act(async () => {
      await toggle.props.onClick();
    });

    expect(renderer!.root.findAllByProps({ className: "chat-tool-row is-complete" })).toHaveLength(1);
    expect(renderer!.root.findAllByProps({ className: "chat-markdown" })).toHaveLength(1);
    renderer!.unmount();
  });

  test("任何历史 Turn 都显示详情菜单，即使没有工具项", () => {
    const value = conversation("done");
    value.turnId = "job_1";
    value.turnRevision = 1;
    value.turnItemsView = "full";
    value.events = [];

    const oldTurnHtml = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={value}
        showRawDetails={false}
        isLastTurn={false}
        sessionBusy={false}
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onLoadToolDetails={async () => {}}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );
    expect(oldTurnHtml).toContain('aria-label="工具详情"');
    expect(oldTurnHtml).toContain('class="chat-assistant-avatar is-tool-trigger"');
    expect(oldTurnHtml).not.toContain("chat-tool-detail-menu-button");
    expect(oldTurnHtml).toContain("原始回答");

    const currentTurnHtml = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={value}
        showRawDetails={false}
        isLastTurn
        sessionBusy={false}
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onLoadToolDetails={async () => {}}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );
    expect(currentTurnHtml).toContain('aria-label="工具详情"');
    expect(currentTurnHtml).toContain('class="chat-assistant-avatar is-tool-trigger"');
    expect(currentTurnHtml).not.toContain("chat-tool-detail-menu-button");
    expect(currentTurnHtml).toContain("原始回答");
  });

  test("点击任意历史 Turn 的助手头像打开菜单并加载工具详情", async () => {
    const value = conversation("done");
    value.turnId = "job_1";
    value.turnRevision = 1;
    value.turnItemsView = "summary";
    value.events = [
      {
        ...value.events[0],
        event_id: "tool_start",
        part_id: "call_1",
        type: "tool_call_start",
        payload: { tool_name: "read_fixture", args: { path: "fixture.json" } },
      },
    ];
    let loadCount = 0;
    let renderer: ReactTestRenderer;

    await act(async () => {
      renderer = create(
        <ChatTurn
          {...mediaProps}
          conversation={value}
          showRawDetails={false}
          isLastTurn={false}
          sessionBusy={false}
          onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
          onLoadToolDetails={async () => {
            loadCount += 1;
          }}
          onReplayTurn={async () => {}}
          {...pendingActionProps}
        />,
      );
    });

    const avatar = renderer!.root.find(
      (node) => node.props.className === "chat-assistant-avatar is-tool-trigger",
    );
    expect(avatar.props.role).toBe("button");
    expect(renderer!.root.findAllByProps({ role: "menuitem" })).toHaveLength(0);

    await act(async () => {
      avatar.props.onClick();
    });
    const menuItem = renderer!.root.findByProps({ role: "menuitem" });
    await act(async () => {
      menuItem.props.onClick();
    });

    expect(loadCount).toBe(1);
    expect(renderer!.root.findAllByProps({ role: "menuitem" })).toHaveLength(0);
    renderer!.unmount();
  });

  test("空内部用户消息默认隐藏原文并提供消息操作菜单", () => {
    const value = conversation("done");
    value.userMessage = {
      ...value.userMessage!,
      content: "",
      metadata: { internal: true },
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('aria-label="空用户消息，右键展开原始消息"');
    expect(html).toContain('aria-label="消息操作"');
    expect(html).toContain("codicon-add");
    expect(html).not.toContain("<system_reminder>原始消息</system_reminder>");
  });

  test("最后一个完成轮次展示内联编辑和重新生成入口", () => {
    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={conversation("done")}
        showRawDetails={false}
        isLastTurn
        sessionBusy={false}
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain('aria-label="消息操作"');
    expect(html).toContain("codicon-add");
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
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain('aria-label="消息操作"');
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
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("重试失败轮次");
  });

  test("失败轮次重试不被过期的会话 busy 状态拦截", async () => {
    let replayCount = 0;
    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(
        <ChatTurn
          {...mediaProps}
          conversation={conversation("error", "job_failed")}
          showRawDetails={false}
          isLastTurn
          sessionBusy
          onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
          onReplayTurn={async () => {
            replayCount += 1;
          }}
          {...pendingActionProps}
        />,
      );
    });

    const retryButton = renderer!.root.findByProps({
      className: "chat-failed-retry-button",
    });
    expect(retryButton.props.disabled).toBe(false);
    act(() => retryButton.props.onClick());
    const confirmButton = renderer!.root.findAll(
      (node) => node.type === "button" && node.children.includes("确认重试"),
    )[0];
    await act(async () => confirmButton.props.onClick());
    expect(replayCount).toBe(1);
    renderer!.unmount();
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
    value.responseParts = [{
      ...value.responseParts![0],
      text: "完整团队汇报：团队、成员、任务状态、子会话和审查结论均已确认。",
    }];

    const html = renderToStaticMarkup(
      <ChatTurn
        {...mediaProps}
        conversation={value}
        showRawDetails={false}
        isLastTurn
        sessionBusy={false}
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("完整团队汇报");
    expect(html).not.toContain("已查看团队面板");
  });

  test("待处理消息展示策略、编辑和撤回操作", () => {
    const value = conversation("queued");
    value.pending = true;
    value.deliveryPolicy = "after_interrupt";
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
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("中断边界后投递");
    expect(html).toContain('title="编辑待处理消息"');
    expect(html).toContain('title="从 FIFO 队列撤回"');
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
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
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
        onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
        onReplayTurn={async () => {}}
        {...pendingActionProps}
      />,
    );

    expect(html).toContain("会话生成");
    expect(html).not.toContain("generated_session_result");
  });
});
