import { describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import ChatPanel, { transcriptConversationsForDisplay } from "./ChatPanel";
import type { ConversationView } from "../../types/frontend";
import * as jobs from "../../api/jobs";
import type { MessageStreamActivity } from "../../state/messageStream/types";

function emptyPanelProps(onRetryHistory: () => void) {
  return {
    apiPort: 8014,
    workspaceId: "workspace-long",
    sessionId: "session-long",
    threadId: "thread-main",
    conversations: [],
    expandDetails: false,
    hasActiveSession: true,
    hasNewerMessages: false,
    hasOlderMessages: false,
    loadingNewerMessages: false,
    loadingOlderMessages: false,
    historyLoading: false,
    projectionState: "ready" as const,
    historyError: "Turn 投影索引损坏：manifest ordinal 不连续",
    onLoadAroundTurn: async () => {},
    onLoadNewerMessages: async () => {},
    onLoadOlderMessages: async () => {},
    onLoadTurnDetails: async () => {},
    onLoadAgentStateMessageRawContent: async () => "",
    onRetryHistory,
    onReplayTurn: async () => {},
    onRemovePending: async () => {},
    onChangePendingPolicy: async () => {},
  };
}

describe("ChatPanel 历史错误", () => {
  test("空时间线不会伪装成暂无历史，并提供明确重试", () => {
    let retries = 0;
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChatPanel {...emptyPanelProps(() => { retries += 1; })} />,
      );
    });

    const alerts = renderer!.root.findAllByProps({ role: "alert" });
    expect(alerts).toHaveLength(1);
    expect(alerts[0].children.join("")).toContain("manifest ordinal 不连续");
    expect(renderer!.root.findAll(
      (node) => node.children.includes("该会话暂无历史消息"),
    )).toHaveLength(0);

    const retryButton = renderer!.root.findByProps({ children: "重试加载" });
    act(() => retryButton.props.onClick());
    expect(retries).toBe(1);
    act(() => renderer!.unmount());
  });

  test("partial 空时间线明确显示迁移状态", () => {
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChatPanel
          {...emptyPanelProps(() => undefined)}
          historyError={null}
          projectionState="partial"
        />,
      );
    });

    expect(renderer!.root.findAll(
      (node) => node.children.includes("旧 Turn 正在迁移"),
    )).toHaveLength(1);
    expect(renderer!.root.findAll(
      (node) => node.children.includes("该会话暂无历史消息"),
    )).toHaveLength(0);
    act(() => renderer!.unmount());
  });

});

test("只有当前线程的活动确认等待才读取，终态与历史不触发查询", async () => {
  const read = spyOn(jobs, "getThreadToolApprovals").mockResolvedValue([]);
  const activity: MessageStreamActivity = {
    activity_id: "wait-one", kind: "approval.wait", status: "waiting",
    scope_ref: "turn", cancellable: true, resumable: false,
    side_effect_policy: "none", resource_refs: [], detail_available: false,
  };
  const waiting: ConversationView = {
    conversationId: "live-turn", sessionId: "session-long", threadId: "thread-main",
    displayMode: "live", source: "pending", pending: true, status: "running",
    jobId: "job-one", userMessage: null, events: [],
    messageStream: {
      connectionStatus: "connected", streamStatus: "open", lastEventSeq: 10,
      failure: null, resumable: false, activities: [activity],
    },
  };
  let renderer: ReactTestRenderer | undefined;
  async function render(conversations: ConversationView[], threadId = "thread-main") {
    await act(async () => {
      const element = <ChatPanel {...emptyPanelProps(() => {})} historyError={null}
        conversations={conversations} threadId={threadId} />;
      if (renderer) renderer.update(element);
      else renderer = create(element);
    });
  }
  try {
    await render([]);
    await render([{ ...waiting, messageStream: undefined }]);
    await render([{ ...waiting, threadId: "other-thread" }]);
    await render([{ ...waiting, sessionId: "other-session" }]);
    await render([{ ...waiting, displayMode: "history", source: "turn" }]);
    await render([{ ...waiting, status: "done" }]);
    await render([{ ...waiting, messageStream: { ...waiting.messageStream!, activities: [{ ...activity, status: "completed" }] } }]);
    expect(read).not.toHaveBeenCalled();

    // SSE 与刷新恢复的快照都通过同一 ConversationView 入口。
    await render([waiting]);
    expect(read).toHaveBeenCalledTimes(1);
    expect(read.mock.calls[0].slice(0, 4)).toEqual([8014, "workspace-long", "session-long", "thread-main"]);
    await render([waiting]);
    expect(read).toHaveBeenCalledTimes(1);
    const firstSignal = read.mock.calls[0][4];
    const next = { ...waiting, messageStream: { ...waiting.messageStream!, activities: [{ ...activity, activity_id: "wait-two" }] } };
    await render([next]);
    expect(firstSignal?.aborted).toBe(true);
    expect(read).toHaveBeenCalledTimes(2);
    const nextSignal = read.mock.calls[1][4];
    await render([next], "other-thread");
    expect(nextSignal?.aborted).toBe(true);
    expect(read).toHaveBeenCalledTimes(2);
    await render([waiting]);
    expect(read).toHaveBeenCalledTimes(3);
    const restoredSignal = read.mock.calls[2][4];
    await render([{ ...waiting, status: "error" }]);
    expect(restoredSignal?.aborted).toBe(true);
  } finally {
    if (renderer) act(() => renderer!.unmount());
    read.mockRestore();
  }
});

describe("ChatPanel transcript 投影", () => {
  test("排队消息不进入 transcript，正在执行的消息仍然保留", () => {
    const queued = {
      pending: true,
      activeJobOverlay: false,
    } as ConversationView;
    const active = {
      pending: true,
      activeJobOverlay: true,
    } as ConversationView;
    const history = {
      pending: false,
      activeJobOverlay: false,
    } as ConversationView;

    expect(transcriptConversationsForDisplay([queued, active, history])).toEqual([
      active,
      history,
    ]);
  });

  test("空历史保留显式 owner，切换线程不显示轮次跳转表单", () => {
    let renderer: ReactTestRenderer;
    act(() => { renderer = create(<ChatPanel {...emptyPanelProps(() => undefined)} />); });
    expect(renderer!.root.findAllByType("form")).toHaveLength(0);
    expect(renderer!.root.findAllByType("datalist")).toHaveLength(0);
    act(() => renderer!.update(<ChatPanel {...emptyPanelProps(() => undefined)} threadId="thread-next" />));
    expect(renderer!.root.findAllByType("form")).toHaveLength(0);
    const shell = renderer!.root.findByProps({ className: "chat-stream-virtual-shell" });
    expect(shell.props["data-owner-session-id"]).toBe("session-long");
    expect(shell.props["data-owner-thread-id"]).toBe("thread-next");
    act(() => renderer!.unmount());
  });
});
