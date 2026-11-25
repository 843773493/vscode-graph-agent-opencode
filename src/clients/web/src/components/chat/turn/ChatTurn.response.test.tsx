import { TranscriptTestView } from "../../../tests/fixtures/transcriptView";
import { describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import OriginalChatTurn from "./ChatTurn";
import { chatTurnProps, conversation } from "./chatTurnTestFixtures";

describe("ChatTurn response 展示", () => {
  test("最终媒体按单Item补读并在原定位节点展示，不藏入过程或降为Markdown", async () => {
    const value = conversation("done");
    value.responseItems = [{id: "image", kind: "content", projection: "summary", status: "completed",
      final: true, carrier_type: "image_url", content: null,
      source: {item_id: "image", message_sequence: 2, item_sequence: 2}}];
    const calls: Parameters<NonNullable<ReturnType<typeof chatTurnProps>["onLoadTurnDetails"]>>[] = [];
    const props = () => ({...chatTurnProps(value), onLoadTurnDetails: async (...args: typeof calls[number]) => {calls.push(args);}});
    const renderer = create(<ChatTurn {...props()} />);
    const section = renderer.root.findByProps({className: "chat-response-bubble chat-output-content"});
    const node = section.props["data-transcript-node"];
    expect(JSON.parse(node)).toEqual({source_ref: {kind: "item", id: "image"}, slot: "body"});
    await act(async () => section.findByType("button").props.onClick());
    expect(calls).toHaveLength(1);
    expect(calls[0]![3]).toContain("final_response");
    expect(calls[0]![5]).toMatchObject({sourceViewId: "view_fixture", sourceViewRevision: 1, itemIds: ["image"]});
    value.responseItems = [{...value.responseItems[0]!, projection: "detail",
      content: {type: "image_url", image_url: {url: "https://example.test/image.png"}}}];
    await act(async () => renderer.update(<ChatTurn {...props()} />));
    expect(renderer.root.findByType("img").props.src).toBe("https://example.test/image.png");
    expect(renderer.root.findByProps({className: "chat-response-bubble chat-output-content"}).props["data-transcript-node"]).toBe(node);
    expect(renderer.root.findAllByProps({className: "chat-thinking-body"})).toHaveLength(0);
    renderer.unmount();
  });

  test("只有权威 final 独立展示，长中间消息不会被猜作最终回答", () => {
    const value = conversation("done");
    value.responseItems = [
      {id:"middle", kind:"text", projection:"detail", status:"completed",
        source:{item_id:"item_middle", item_sequence:2, message_sequence:2, }, text:"长中间消息".repeat(5000)},
      {...value.responseItems![0], text:"权威最终回答"},
    ];
    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
    expect(html).toContain("权威最终回答");
    expect(html).not.toContain("长中间消息");
  });

  test("回复正文使用圆角气泡，摘要独立展示且没有头像占位", () => {
    const value = conversation("done");
    value.turnId = "turn-compact";
    value.activityStats = { duration_ms: 27000, item_count: 1 };
    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('class="chat-assistant-row chat-assistant-content"');
    expect(html).toContain('class="chat-markdown chat-response-bubble"');
    expect(html).toContain("原始回答");
    expect(html).toContain('class="chat-thinking chat-turn-activity');
    expect(html).not.toContain("chat-assistant-avatar");
    expect(html).not.toContain("codicon-copilot");
  });

  test("消息流关闭后未经 final 确认的正文仍留在过程内", () => {
    const value = conversation("done");
  value.displayMode = "live";
  value.source = "pending";
  value.responseItems = [];
  value.messageStream = {
    connectionStatus: "terminal",
    streamStatus: "completed",
    lastEventSeq: 3,
    failure: null,
    resumable: false,
  };
  value.assistantMessages = [];
  value.responseItems = [{
    id: "part_bounded_end",
    kind: "text",
    projection: "streaming",
    status: "completed",
    source: { message_sequence: 0 },
    text: "Turn detail 最终正文",
    final: false,
  }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).not.toContain("Turn detail 最终正文");
    expect(html).toContain('aria-expanded="false"');
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

  test("流式正文使用实时内容，摘要从首段回复到完成始终位于正文前", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
  value.responseItems = [{
    ...value.responseItems![0],
    text: "旧历史正文",
  }];
  value.messageStream = {
    connectionStatus: "connected",
    streamStatus: "open",
    lastEventSeq: 3,
    failure: null,
    resumable: true,
  };
  value.responseItems = [{
    id: "live_text",
    kind: "text",
    projection: "streaming",
    status: "running",
    source: { message_sequence: 0 },
    text: "实时正文",
    final: false,
  }];
  value.assistantMessages = [];

    for (const status of ["running", "done"] as const) {
      value.status = status;
      const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
      expect(html).toContain("实时正文");
      expect(html).not.toContain("旧历史正文");
      const summaryPosition = html.indexOf('class="chat-thinking chat-turn-activity');
      expect(summaryPosition).toBeGreaterThanOrEqual(0);
      expect(summaryPosition).toBeLessThan(html.indexOf("实时正文"));
      if (status === "running") {
        expect(html).toContain('aria-expanded="true"');
        expect(html).not.toContain("耗时 —");
      }
    }
  });

  test("运行活动只在过程标题展示，默认不挂载第三层 reasoning 正文", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "open",
      lastEventSeq: 4,
      failure: null,
      activeState: {
        kind: "model_output",
        phase: "reasoning",
        entity_id: "reasoning_block_1",
        carrier_type: "reasoning_items",
        block_id: "reasoning_block_1",
        status: "running",
      },
      resumable: true,
    };
    const rawReasoning = "I am checking the repository before answering.";
    value.responseItems = [{
      id: "reasoning-live",
      kind: "reasoning",
      projection: "streaming",
      status: "running",
      source: { message_sequence: 1, content_block_index: 0 },
      text: rawReasoning,
      carrier_type: "reasoning_items",
      final: false,
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).not.toContain('data-status-kind="work-summary"');
    expect(html.match(/class="chat-activity-current"/g)).toHaveLength(1);
    expect(html).not.toContain(rawReasoning);
    expect(html).toContain("chat-activity-group");
  });

  test("用户中断显示为中性状态且不重复渲染取消事件", () => {
    const value = conversation("error", "session_interrupted");
  value.displayMode = "live";
  value.source = "pending";
  value.responseItems = [];
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
    expect(html.match(/>已由用户中断</g)?.length).toBe(1);
  });

  test("压缩 Activity 的终态保留在消息正文下方", () => {
    const value = conversation("done");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "completed",
      lastEventSeq: 12,
      failure: null,
      activeState: {
        // 后端终态 active_state 的 kind 恒为 "terminal"，旧 fixture 的
        // "stream" 是已下线字面量的过期残留，不参与任何判断。
        kind: "terminal",
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
    // 终态 active_state 的 kind 由后端 _set_terminal_active_state 恒写作
    // "terminal"。这条断言把 fixture 钉在后端真实字面量上，防止它再次回退到
    // 已下线的 "stream"（组件当前不读该字段，故渲染结果不能发现回退）。
    expect(value.messageStream.activeState?.kind).toBe("terminal");
  });

  test("进行中的 Activity 不再占用消息正文", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
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

    expect(html).not.toContain("正在压缩上下文");
    expect(html).not.toContain('class="chat-inline-activity is-running"');
    expect(html).not.toContain('data-activity-id="compaction_running"');
  });

  test("终态消息流展示所有通用 Activity 的结果", () => {
    const value = conversation("done");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
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

  test("正在中断提示移入状态栏，正文不提前显示已中断", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "interrupting",
      lastEventSeq: 19,
      failure: null,
      resumable: true,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).not.toContain('data-status-kind="interrupting"');
    expect(html).not.toContain("正在中断本轮任务");
    expect(html).not.toContain("正在等待模型、工具和 Activity 完成停止确认");
    expect(html).not.toContain("已由用户中断");
  });

  test("消息流失败时区分执行丢失，并在缺少失败详情时仍显示错误", () => {
    const lost = conversation("error");
    lost.displayMode = "live";
    lost.source = "pending";
    lost.responseItems = [];
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
    missing.responseItems = [];
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
    value.responseItems = [];
    value.activityStats = { duration_ms: 30, item_count: 2 };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("本轮执行失败");
    expect(html).toContain("该记录未保存失败详情");
    expect(html).toContain('data-status-kind="turn-failed"');
  });

  test("连接缺口和协议诊断不重复到正文", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
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

    expect(html).not.toContain("实时消息流出现缺口，正在读取权威快照补齐");
    expect(html).not.toContain("诊断：消息流 event_seq 不连续: expected=8 actual=10");
  });

  test("消息流断开提示移入状态栏", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
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

    expect(html).not.toContain("实时消息流已断开，正在重连");
    expect(html).not.toContain("诊断：SSE 心跳超时");
  });

  test("消息流耗尽重连上限不作为正文的执行终态", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "retry_exhausted",
      streamStatus: "open",
      lastEventSeq: 8,
      failure: null,
      protocolError: "无法连接 Turn 消息流: 404 Not Found",
      resumable: true,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).not.toContain("已停止自动重连");
    expect(html).not.toContain("实时消息流已断开，正在重连");
    expect(html).not.toContain("诊断：无法连接 Turn 消息流: 404 Not Found");
  });

  test("缺少 Activity 实体的实时状态不占用正文", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseItems = [];
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

    expect(html).not.toContain('data-status-kind="activity"');
    expect(html).not.toContain("正在处理 Activity");
    expect(html).not.toContain("provider.private 的详细状态暂不可用");
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
    value.responseItems![0] = {
      ...value.responseItems![0],
      kind: "text",
      text: "已提交的半截回答",
      partial: true,
      completion_reason: "user_interrupt",
      final: false,
    };

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html.match(/>已由用户中断</g)?.length).toBe(1);
    expect(html).not.toContain("生成已中断");
  });

  test("完成态工具只显示统计而不暴露结果正文", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.status = "done";
    value.turnStatus = "completed";
    value.assistantMessages = [];
    value.messageStream = {
      connectionStatus: "connected",
      streamStatus: "open",
      lastEventSeq: 22,
      failure: null,
      resumable: true,
    };
    value.responseItems = [{
      id: "unknown-tool-execution",
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
    expect(html).not.toContain("耗时 —");
    expect(html).not.toContain("shell 结果未知");
    expect(html).not.toContain("chat-thinking-body");
  });

  test("完成态 reasoning 折叠行只显示耗时和 Item 计数", () => {
    const value = conversation("done");
    value.displayMode = "live";
    value.source = "pending";
    value.activityStats = { duration_ms: 7100, item_count: 4 };
    value.assistantMessages = [{
      ...value.assistantMessages![0],
      content: "OK",
    }];
    value.responseItems = [
      {
        id: "reasoning-complete",
        kind: "reasoning",
        projection: "streaming",
        status: "completed",
        source: { message_sequence: 1 },
        text: "Confirming test tool success with results",
        final: false,
      },
      {
        id: "final-complete",
        kind: "final_text",
        projection: "streaming",
        status: "completed",
        source: { message_sequence: 2 },
        text: "OK",
        final: true,
      },
    ];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("耗时 7.1s");
    expect(html).toContain("Item 4 项");
    expect(html).not.toContain("Confirming test tool success with results");
    expect(html).not.toContain("chat-thinking-body");
    expect(html).toContain("OK");
  });
});

function ChatTurn(props: React.ComponentProps<typeof OriginalChatTurn>) { return <TranscriptTestView><OriginalChatTurn {...props} /></TranscriptTestView>; }
