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

  test("压缩 Activity 的运行中和终态都显示在消息正文下方", () => {
    const value = conversation("done");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "completed",
      lastEventSeq: 12,
      failure: null,
      activeState: {
        kind: "stream",
        phase: "completed",
        entity_id: "stream",
        status: "completed",
      },
      activities: [
        {
          activity_id: "compaction_1",
          kind: "context.compaction",
          scope_ref: "turn",
          status: "completed",
          summary: "第一次压缩完成",
          cancellable: false,
          resumable: false,
          side_effect_policy: "none",
          resource_refs: [],
          detail_available: true,
        },
        {
          activity_id: "compaction_2",
          kind: "context.compaction",
          scope_ref: "turn",
          status: "failed",
          cancellable: false,
          resumable: false,
          side_effect_policy: "none",
          resource_refs: [],
          detail_available: false,
        },
      ],
      resumable: false,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("第一次压缩完成");
    expect(html).toContain("上下文压缩失败");
    expect(html).toContain('data-activity-id="compaction_1"');
    expect(html).toContain('data-activity-id="compaction_2"');
  });

  test("进行中的压缩 Activity 显示实时状态", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "open",
      lastEventSeq: 8,
      failure: null,
      activeState: {
        kind: "activity",
        phase: "running",
        entity_id: "compaction_running",
        activity_id: "compaction_running",
        activity_kind: "context.compaction",
        status: "running",
      },
      activities: [
        {
          activity_id: "compaction_running",
          kind: "context.compaction",
          scope_ref: "turn",
          status: "running",
          cancellable: false,
          resumable: false,
          side_effect_policy: "none",
          resource_refs: [],
          detail_available: false,
        },
      ],
      resumable: true,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("正在压缩上下文");
    expect(html).toContain('class="chat-inline-activity is-running"');
    expect(html).toContain('data-activity-id="compaction_running"');
  });

  test("终态消息流展示所有通用 Activity 的结果", () => {
    const value = conversation("done");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "completed",
      lastEventSeq: 18,
      failure: null,
      activities: [
        {
          activity_id: "approval_done",
          kind: "approval.wait",
          scope_ref: "turn",
          status: "completed",
          cancellable: false,
          resumable: false,
          side_effect_policy: "none",
          resource_refs: [],
          detail_available: false,
        },
        {
          activity_id: "unknown_provider",
          kind: "provider.private",
          scope_ref: "turn",
          status: "unknown",
          cancellable: false,
          resumable: false,
          side_effect_policy: "unknown",
          resource_refs: [],
          detail_available: false,
        },
      ],
      resumable: false,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("审批已完成");
    expect(html).toContain("结果未知 provider.private");
    expect(html).toContain('data-activity-id="approval_done"');
    expect(html).toContain('data-activity-id="unknown_provider"');
  });

  test("消息流进入 interrupting 时显示正在中断而不是已中断", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "interrupting",
      lastEventSeq: 19,
      failure: null,
      resumable: true,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('data-status-kind="interrupting"');
    expect(html).toContain("正在中断本轮任务");
    expect(html).toContain("正在等待模型、工具和 Activity 完成停止确认");
    expect(html).not.toContain("已由用户中断");
  });

  test("消息流失败时区分执行丢失，并在缺少失败详情时仍显示错误", () => {
    const lost = conversation("error");
    lost.displayMode = "live";
    lost.source = "pending";
    lost.responseParts = [];
    lost.assistantMessages = [];
    lost.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "failed",
      lastEventSeq: 20,
      failure: {
        code: "execution_lost",
        message: "工作区后端已退出",
        afterInterruptRequested: true,
        resumable: false,
      },
      resumable: false,
    };
    const lostHtml = renderToStaticMarkup(<ChatTurn {...chatTurnProps(lost)} />);
    expect(lostHtml).toContain("执行丢失");
    expect(lostHtml).toContain("工作区后端已退出");
    expect(lostHtml).toContain("重试本轮");
    expect(lostHtml).toContain("原 AgentLoop 已安全终止");

    const missing = conversation("error");
    missing.displayMode = "live";
    missing.source = "pending";
    missing.responseParts = [];
    missing.assistantMessages = [];
    missing.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "failed",
      lastEventSeq: 21,
      failure: null,
      resumable: false,
    };
    const missingHtml = renderToStaticMarkup(<ChatTurn {...chatTurnProps(missing)} />);
    expect(missingHtml).toContain("消息流失败，但后端没有提供失败详情");
  });

  test("历史失败 Turn 在中间消息折叠时也显示终态", () => {
    const value = conversation("error", "job_failed");
    value.turnId = "job_failed_history";
    value.turnStatus = "failed";
    value.assistantMessages = [];
    value.responseParts = [];
    value.activityStats = { duration_ms: 30, message_count: 2 };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("本轮执行失败");
    expect(html).toContain("后端没有提供可用的失败详情");
    expect(html).toContain('data-status-kind="turn-failed"');
  });

  test("消息流连接缺口和协议诊断会对用户可见", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "gap",
      streamStatus: "open",
      lastEventSeq: 7,
      failure: null,
      protocolError: "消息流 event_seq 不连续: expected=8 actual=10",
      resumable: true,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("实时消息流出现缺口，正在请求 snapshot 恢复");
    expect(html).toContain("诊断：消息流 event_seq 不连续: expected=8 actual=10");
  });

  test("消息流断开时保留重连提示和协议诊断", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "disconnected",
      streamStatus: "open",
      lastEventSeq: 8,
      failure: null,
      protocolError: "SSE 心跳超时",
      resumable: true,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("实时消息流已断开，正在重连");
    expect(html).toContain("诊断：SSE 心跳超时");
  });

  test("active_state 缺少 Activity 实体时显示通用回退", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "open",
      lastEventSeq: 8,
      failure: null,
      activeState: {
        kind: "activity",
        phase: "running",
        entity_id: "missing_activity",
        activity_id: "missing_activity",
        activity_kind: "provider.private",
        status: "running",
      },
      activities: [],
      resumable: true,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('data-status-kind="activity"');
    expect(html).toContain("正在处理 Activity");
    expect(html).toContain("provider.private 的详细状态暂不可用");
  });

  test("历史 snapshot 已提供中断事实时不重复显示历史取消行", () => {
    const value = conversation("error");
    value.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "interrupted",
      lastEventSeq: 9,
      failure: null,
      resumable: false,
    };
    value.turnStatus = "cancelled";
    value.responseParts![0] = {
      ...value.responseParts![0],
      kind: "text",
      text: "已提交的半截回答",
      partial: true,
      completion_reason: "user_interrupt",
      final: false,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html.match(/已由用户中断/g)?.length).toBe(1);
    expect(html).not.toContain("生成已中断");
  });

  test("未知工具结果显示结果未知而不是成功或运行中", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.status = "done";
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "open",
      lastEventSeq: 22,
      failure: null,
      resumable: true,
    };
    value.responseParts = [{
      part_id: "unknown-tool-execution",
      kind: "tool_call",
      projection: "streaming",
      status: "completed",
      source: { message_sequence: 0 },
      tool_call_id: "call-unknown",
      tool_name: "shell",
      arguments: '{"command":"touch side-effect"}',
      outcome_unknown: true,
      final: true,
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("shell 结果未知");
    expect(html).not.toContain("正在运行 shell");
    expect(html).not.toContain("已运行 shell");
  });

  test("历史 Turn 折叠行提示边界，展开后在中间消息中显示未知工具结果", async () => {
    const value = conversation("error");
    value.turnId = "boundary-turn-0003";
    value.turnStatus = "failed";
    value.responseParts = [{
      part_id: "tool-call:2:0",
      kind: "tool_call",
      projection: "summary",
      status: "failed",
      source: {
        message_sequence: 2,
        assistant_message_sequence: 2,
        call_index: 0,
      },
      tool_call_id: "call-boundary-unknown-tool",
      tool_name: "large_test_output",
      arguments: null,
      text: "pending",
      outcome_unknown: true,
      final: false,
    }];

    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<ChatTurn {...chatTurnProps(value)} />);
    });

    const toggle = renderer!.root.findByProps({ className: "chat-thinking-toggle" });
    expect(toggle.props["aria-label"]).toBe(
      "展开 Turn 中间消息（工具执行结果未知）",
    );
    expect(renderer!.root.findAllByProps({ "data-status-kind": "tool-outcome-unknown" })).toHaveLength(1);
    expect(renderer!.root.findAllByProps({ className: "chat-inline-tool-unknown" })).toHaveLength(0);

    await act(async () => {
      await toggle.props.onClick();
    });

    const body = renderer!.root.findByProps({ className: "chat-thinking-body" });
    expect(body.findAllByProps({ "data-status-kind": "tool-outcome-unknown" })).toHaveLength(0);
    expect(body.findByProps({ className: "chat-tool-row is-unknown" })).toBeDefined();
    expect(JSON.stringify(renderer!.toJSON())).toContain(
      "large_test_output 结果未知",
    );
    renderer!.unmount();
  });

  test("没有 Assistant 正文的稳定边界 Turn 末尾也显示回复操作", () => {
    const value = conversation("error");
    value.turnId = "boundary-turn-without-response";
    value.turnItemsView = "full";
    value.assistantMessages = [];
    value.responseParts = [{
      part_id: "tool-call:without-response",
      kind: "tool_call",
      projection: "summary",
      status: "failed",
      source: { message_sequence: 2, call_index: 0 },
      tool_call_id: "call-without-response",
      tool_name: "invoke_custom_tool",
      outcome_unknown: true,
      final: false,
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('aria-label="回复操作"');
    expect(html).toContain('aria-label="复制（暂无可复制内容）"');
    expect(html).toContain('aria-label="有帮助（暂未开放）"');
    expect(html).toContain('aria-label="没有帮助（暂未开放）"');
    expect(html).not.toContain("重新生成最后回复");
  });

  test("完整历史回复和摘要 Turn 都提供复制和反馈且不显示重新生成", () => {
    const value = conversation("done");
    value.turnId = "history-turn-0001";
    value.turnItemsView = "full";

    const historicalHtml = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
    expect(historicalHtml).toContain('aria-label="回复操作"');
    expect(historicalHtml).toContain('aria-label="复制"');
    expect(historicalHtml).toContain('aria-label="有帮助（暂未开放）"');
    expect(historicalHtml).toContain('aria-label="没有帮助（暂未开放）"');

    const summaryValue = {
      ...value,
      turnItemsView: "summary" as const,
    };
    const summaryHtml = renderToStaticMarkup(
      <ChatTurn {...chatTurnProps(summaryValue)} isLastTurn />,
    );
    expect(summaryHtml).toContain('aria-label="回复操作"');
    expect(summaryHtml).toContain('aria-label="复制"');
    expect(summaryHtml).toContain('aria-label="有帮助（暂未开放）"');
    expect(summaryHtml).toContain('aria-label="没有帮助（暂未开放）"');
    expect(summaryHtml).not.toContain('aria-label="重新生成最后回复"');
  });

  test("用户中断优先于半截工具的未知结果", () => {
    const value = conversation("error");
    value.turnStatus = "cancelled";
    value.responseParts = [
      {
        part_id: "partial-text",
        kind: "text",
        projection: "detail",
        status: "completed",
        source: { message_sequence: 2 },
        text: "我准备读取配置文件。",
        completion_reason: "user_interrupt",
        partial: true,
        final: false,
      },
      {
        part_id: "partial-tool",
        kind: "tool_call",
        projection: "summary",
        status: "failed",
        source: {
          message_sequence: 2,
          assistant_message_sequence: 2,
          call_index: 0,
        },
        tool_call_id: "call-partial-tool",
        tool_name: "read_file",
        outcome_unknown: true,
        final: false,
      },
    ];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('data-status-kind="user-interrupted"');
    expect(html).toContain("已由用户中断");
    expect(html).not.toContain("工具执行结果未知");
    expect(html).not.toContain("后端未返回结果，无法确认是否成功");
  });

  test("历史已知工具失败显示失败状态而不是未知结果", () => {
    const value = conversation("error");
    value.turnStatus = "failed";
    value.responseParts = [{
      part_id: "failed-tool",
      kind: "tool_call",
      projection: "summary",
      status: "failed",
      source: {
        message_sequence: 2,
        assistant_message_sequence: 2,
        call_index: 0,
      },
      tool_call_id: "call-failed-tool",
      tool_name: "read_file",
      text: "Permission denied",
      outcome_unknown: false,
      final: false,
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('data-status-kind="tool-failed"');
    expect(html).toContain("工具执行失败");
    expect(html).toContain("read_file：工具返回了失败结果");
    expect(html).not.toContain("工具执行结果未知");
  });

  test("历史失败但没有工具细节时仍显示通用失败状态", () => {
    const value = conversation("error");
    value.turnStatus = "failed";
    value.responseParts = [];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('data-status-kind="turn-failed"');
    expect(html).toContain("本轮执行失败");
    expect(html).toContain("后端没有提供可用的失败详情");
  });

  test("回放后的新 Turn 明确显示上下文已回退", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.pending = true;
    value.userMessage = {
      ...value.userMessage!,
      metadata: {
        replay_action: "edit_and_continue",
        source: "optimistic_replay",
      },
    };
    value.assistantMessages = [];
    value.responseParts = [];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("已回退上下文，从编辑后的消息继续");
    expect(html).toContain("工作区文件修改不会被撤销");
    expect(html).toContain('data-status-kind="rewind"');
  });

  test("非用户中断的取消事件显示内部执行错误而不是用户中断", () => {
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

    expect(html).toContain("内部执行取消");
    expect(html).toContain("AgentLoop 在没有用户中断请求的情况下被取消");
    expect(html).not.toContain("任务已取消");
  });

  test("总超时事件显示超时而不是任务取消", () => {
    const value = conversation("error", "job_failed");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
    value.turnStatus = "timed_out";
    value.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "failed",
      lastEventSeq: 4,
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

    expect(html).toContain("本轮执行超时");
    expect(html).toContain("AgentLoop 在没有用户中断请求的情况下被取消");
    expect(html).not.toContain("任务已取消");
  });

  test("历史 partial Turn 显示用户中断提示且不展示独立重试入口", () => {
    const value = conversation("error");
    value.turnStatus = "cancelled";
    value.assistantMessages![0] = {
      ...value.assistantMessages![0],
      content: "我已经开始分析这个问题，但回答在这里被用户中断……",
    };
    value.responseParts![0] = {
      ...value.responseParts![0],
      kind: "text",
      text: "我已经开始分析这个问题，但回答在这里被用户中断……",
      completion_reason: "user_interrupt",
      partial: true,
      final: false,
    };

    const html = renderToStaticMarkup(
      <ChatTurn {...chatTurnProps(value)} isLastTurn />,
    );

    expect(html).toContain("我已经开始分析这个问题，但回答在这里被用户中断……");
    expect(html).toContain("已由用户中断");
    expect(html).not.toContain("重新生成用户中断轮次");
    expect(html).not.toContain("生成已中断");
    expect(html).not.toContain("重试失败轮次");
    expect(html).not.toContain("此轮消息无法显示");
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
      "final_response",
    ]);
    expect(renderer!.root.findByProps({ "aria-expanded": true })).toBeTruthy();
    const summaryHtml = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
    expect(summaryHtml).toContain("耗时 1.3s · 消息 5 条");
    expect(summaryHtml).not.toContain("assistant 1");
    expect(summaryHtml).not.toContain("tool 1/1");
    renderer!.unmount();
  });

  test("历史 ToolRow 展开时只请求当前 tool_call_id 的详情", async () => {
    const value = conversation("done");
    value.turnId = "job_1";
    value.turnRevision = 1;
    value.turnItemsView = "summary";
    value.responseParts = [
      {
        part_id: "tool-call:call-1",
        kind: "tool_call",
        projection: "summary",
        status: "completed",
        source: {
          message_sequence: 2,
          assistant_message_sequence: 2,
          call_index: 0,
        },
        tool_call_id: "call-1",
        tool_name: "read_fixture",
      },
      ...value.responseParts!,
    ];
    let loadedToolCallId = "";
    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(
        <ChatTurn
          {...chatTurnProps(value)}
          onLoadToolDetails={async (_turnId, toolCallId) => {
            loadedToolCallId = toolCallId;
          }}
        />,
      );
    });

    const activityToggle = renderer!.root.findByProps({
      "aria-label": "展开 Turn 中间消息",
    });
    await act(async () => {
      await activityToggle.props.onClick();
    });
    const toolToggle = renderer!.root.findByProps({
      className: "chat-tool-summary",
    });
    await act(async () => {
      await toolToggle.props.onClick();
    });

    expect(loadedToolCallId).toBe("call-1");
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

  test("同一模型消息中的普通文本和 tool_call 分别展示", () => {
    const value = conversation("done");
    value.displayMode = "live";
    value.source = "pending";
    value.status = "running";
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "open",
      lastEventSeq: 4,
      failure: null,
      resumable: true,
    };
    value.responseParts = [
      {
        part_id: "text-before-tool",
        kind: "text",
        projection: "streaming",
        status: "completed",
        source: { message_sequence: 1, content_block_index: 0 },
        text: "我先读取文件。",
      },
      {
        part_id: "tool-call-same-message",
        kind: "tool_call",
        projection: "streaming",
        status: "pending",
        source: { message_sequence: 1, assistant_message_sequence: 1, call_index: 0 },
        tool_call_id: "call-same-message",
        tool_name: "read_file",
        arguments: '{"path":"README.md"}',
      },
    ];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
    const textIndex = html.indexOf("我先读取文件");
    const thinkingBodyIndex = html.indexOf('class="chat-thinking-body"');
    expect(textIndex).toBeGreaterThanOrEqual(0);
    expect(thinkingBodyIndex).toBeGreaterThan(textIndex);
    expect(html).toContain("read_file");
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

  test("完成轮次展示内联编辑和回复操作", () => {
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
    expect(html).toContain('aria-label="回复操作"');
    expect(html).toContain('aria-label="复制"');
    expect(html).not.toContain("重新生成最后回复");
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

  test("最后一个失败轮次不展示独立重试入口", () => {
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

    expect(html).not.toContain("重试失败轮次");
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
