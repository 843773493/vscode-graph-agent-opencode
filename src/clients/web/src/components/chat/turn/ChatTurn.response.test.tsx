import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import ChatTurn from "./ChatTurn";
import { chatTurnProps, conversation } from "./chatTurnTestFixtures";

describe("ChatTurn response 展示", () => {
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

  test("默认主线流式 reasoning 只显示可信状态，不挂载原始正文", () => {
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
    value.responseParts = [{
      part_id: "reasoning-live",
      kind: "reasoning",
      projection: "streaming",
      status: "running",
      source: { message_sequence: 1, content_block_index: 0 },
      text: rawReasoning,
      carrier_type: "reasoning_items",
      final: false,
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('data-status-kind="work-summary"');
    expect(html).toContain("正在思考");
    expect(html).not.toContain(rawReasoning);
    expect(html).not.toContain("chat-thinking-body");
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
    value.activityStats = { duration_ms: 30, item_count: 2 };

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

    expect(html).toContain("实时消息流出现缺口，正在读取权威快照补齐");
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

  test("消息流耗尽重连上限时给出可见终态而不是继续显示重连", () => {
    const value = conversation("running");
    value.displayMode = "live";
    value.source = "pending";
    value.responseParts = [];
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

    expect(html).toContain("已停止自动重连");
    expect(html).not.toContain("实时消息流已断开，正在重连");
    expect(html).toContain("诊断：无法连接 Turn 消息流: 404 Not Found");
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

  test("完成态工具只显示统计而不暴露结果正文", () => {
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
    expect(html).toContain("耗时 — · Item 计数同步中");
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
    value.responseParts = [
      {
        part_id: "reasoning-complete",
        kind: "reasoning",
        projection: "streaming",
        status: "completed",
        source: { message_sequence: 1 },
        text: "Confirming test tool success with results",
        final: false,
      },
      {
        part_id: "final-complete",
        kind: "final_text",
        projection: "streaming",
        status: "completed",
        source: { message_sequence: 2 },
        text: "OK",
        final: true,
      },
    ];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain("耗时 7.1s · Item 4 项");
    expect(html).toContain('aria-label="Turn 中间消息：耗时 7.1s · Item 4 项"');
    expect(html).not.toContain("Confirming test tool success with results");
    expect(html).not.toContain("chat-thinking-body");
    expect(html).toContain("OK");
  });
});
