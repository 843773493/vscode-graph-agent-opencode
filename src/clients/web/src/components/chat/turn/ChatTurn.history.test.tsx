import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import ChatTurn from "./ChatTurn";
import {
  chatTurnProps,
  conversation,
  loadAgentStateMessageRawContent,
  mediaProps,
  pendingActionProps,
} from "./chatTurnTestFixtures";

describe("ChatTurn 历史详情", () => {
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
      "展开 Turn 中间消息：耗时 — · Item 计数同步中 · 工具执行结果未知",
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
      tool_name: "invoke_extension_tool",
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

  test("终态历史保留消息流诊断时仍展示已知工具失败", async () => {
    const value = conversation("done");
    value.turnId = "turn-tool-failed";
    value.turnStatus = "completed";
    value.turnItemsView = "full";
    value.activityStats = { duration_ms: 100, item_count: 2 };
    value.messageStream = {
      connectionStatus: "terminal",
      streamStatus: "completed",
      lastEventSeq: 21,
      failure: null,
      resumable: false,
    };
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
    }, {
      part_id: "failed-tool-result",
      kind: "tool_result",
      projection: "detail",
      status: "completed",
      source: {
        message_sequence: 3,
        assistant_message_sequence: 2,
        call_index: 0,
      },
      tool_call_id: "call-failed-tool",
      tool_name: "read_file",
      result: "Permission denied",
      outcome_unknown: false,
      final: true,
    }];

    const html = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);

    expect(html).toContain('data-status-kind="tool-failed"');
    expect(html).toContain("工具执行失败");
    expect(html).toContain("read_file：工具返回了失败结果");
    expect(html).not.toContain("工具执行结果未知");

    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<ChatTurn {...chatTurnProps(value)} />);
    });
    const activity = renderer!.root.findByProps({ "data-status-kind": "tool-failed" });
    const toggle = activity.findByProps({ "aria-expanded": false });
    await act(async () => {
      await toggle.props.onClick();
    });
    expect(renderer!.root.findByProps({ className: "chat-tool-row is-failed" })).toBeTruthy();
    act(() => renderer!.unmount());
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
      item_count: 5,
    };
    let loadedInclude: string[] | undefined;
    let detailLoadCalls = 0;
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChatTurn
          {...chatTurnProps(value)}
          onLoadTurnDetails={async (_turnIds, _identity, _refresh, include) => {
            detailLoadCalls += 1;
            loadedInclude = include;
          }}
        />,
      );
    });
    const toggle = renderer!.root.findByProps({ className: "chat-thinking-toggle" });
    expect(toggle.props["aria-label"]).toBe(
      "展开 Turn 中间消息：耗时 1.3s · Item 5 项",
    );
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
    await act(async () => {
      await toggle.props.onClick();
    });
    await act(async () => {
      await toggle.props.onClick();
    });
    expect(detailLoadCalls).toBe(2);
    const summaryHtml = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
    expect(summaryHtml).toContain("耗时 1.3s · Item 5 项");
    expect(summaryHtml).not.toContain("assistant 1");
    expect(summaryHtml).not.toContain("tool 1/1");
    renderer!.unmount();
  });

  test("没有中间 item 的历史 Turn 只显示统计，不提供空展开区", () => {
    const value = conversation("done");
    value.turnId = "job_plain";
    value.turnRevision = 1;
    value.turnItemsView = "summary";
    value.activityStats = {
      duration_ms: 7100,
      item_count: 0,
    };

    const renderer = create(<ChatTurn {...chatTurnProps(value)} />);
    const activity = renderer.root.findByProps({
      className: "chat-thinking-toggle is-static",
    });
    expect(activity.props.role).toBe("status");
    expect(activity.props["aria-label"]).toBe(
      "Turn 中间消息：耗时 7.1s · Item 0 项",
    );
    expect(renderer.root.findAllByProps({ className: "chat-thinking-body" })).toHaveLength(0);
    expect(renderer.root.findAllByProps({ className: "chat-thinking-empty" })).toHaveLength(0);
    expect(renderer.root.findAllByProps({ "aria-expanded": false })).toHaveLength(0);
    renderer.unmount();
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

    const activityToggle = renderer!.root.findByProps({ className: "chat-thinking-toggle" });
    expect(activityToggle.props["aria-label"]).toBe(
      "展开 Turn 中间消息：耗时 — · Item 计数同步中",
    );
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
    value.assistantMessages![0].content = "最终响应";
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

    const toggle = renderer!.root.findByProps({ className: "chat-thinking-toggle" });
    expect(toggle.props["aria-label"]).toBe(
      "展开 Turn 中间消息：耗时 — · Item 计数同步中",
    );
    await act(async () => {
      await toggle.props.onClick();
    });

    expect(renderer!.root.findAllByProps({ className: "chat-tool-row is-complete" })).toHaveLength(1);
    expect(renderer!.root.findAllByProps({ className: "chat-markdown" })).toHaveLength(1);
    renderer!.unmount();
  });

  test("历史展开与收起保持压缩前后正文的 canonical DOM 顺序", async () => {
    const value = conversation("done");
    value.turnId = "turn_compaction_order";
    value.turnItemsView = "full";
    value.activityStats = { duration_ms: 100, item_count: 4 };
    value.assistantMessages![0].content = "最终响应";
    value.responseParts = [
      { part_id: "before", kind: "reasoning", projection: "detail", status: "completed",
        source: { message_sequence: 1, item_id: "item_before", logical_item_ordinal: 1 }, text: "压缩前思考" },
      { part_id: "summary", kind: "compaction_summary", projection: "detail", status: "completed",
        source: { message_sequence: 2, item_id: "item_summary", logical_item_ordinal: 2 },
        text: "A condensed summary follows:\n\n<summary>\n三轮均通过 read_file 核对 README。\n</summary>" },
      { part_id: "after", kind: "reasoning", projection: "detail", status: "completed",
        source: { message_sequence: 3, item_id: "item_after", logical_item_ordinal: 3 }, text: "压缩后思考" },
      { part_id: "final", kind: "final_text", projection: "detail", status: "completed", final: true,
        source: { message_sequence: 4, item_id: "item_final", logical_item_ordinal: 4 }, text: "最终响应" },
    ];
    const summaryMarkup = renderToStaticMarkup(<ChatTurn {...chatTurnProps(value)} />);
    expect(summaryMarkup).toContain("三轮均通过 read_file 核对 README。");
    expect(summaryMarkup).toContain("&lt;summary&gt;");
    let loads = 0;
    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<ChatTurn {...chatTurnProps(value)} onLoadTurnDetails={async () => { loads += 1; }} />);
    });
    const sources = () => renderer!.root.findAll((node) =>
      typeof node.type === "string" && typeof node.props["data-response-sources"] === "string",
    ).flatMap((node) => JSON.parse(node.props["data-response-sources"]));
    const expected = value.responseParts.map(({ part_id, kind, source }) => ({ part_id, kind, source }));
    expect(sources()).toEqual([expected[1], expected[3]]);
    const toggle = renderer!.root.findByProps({ className: "chat-thinking-toggle" });
    await act(async () => { await toggle.props.onClick(); });
    expect(loads).toBe(1);
    expect(sources()).toEqual(expected);
    await act(async () => { await toggle.props.onClick(); });
    expect(sources()).toEqual([expected[1], expected[3]]);
    expect(renderer!.root.findAllByProps({ className: "chat-thinking-toggle" })).toHaveLength(1);
    act(() => renderer!.unmount());
  });

  test("默认主线同一模型消息只展示普通文本，不展开 tool_call 正文", () => {
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
    expect(thinkingBodyIndex).toBe(-1);
    expect(html).not.toContain("read_file");
  });
});
