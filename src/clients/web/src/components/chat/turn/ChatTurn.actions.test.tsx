import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import ChatTurn from "./ChatTurn";
import {
  chatTurnProps,
  conversation,
  loadAgentStateMessageRawContent,
  mediaProps,
  pendingActionProps,
} from "./chatTurnTestFixtures";

describe("ChatTurn 用户动作", () => {
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
    expect(html).not.toContain('title="编辑待处理消息"');
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
