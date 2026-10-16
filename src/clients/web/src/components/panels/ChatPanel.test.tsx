import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import ChatPanel, { transcriptConversationsForDisplay } from "./ChatPanel";
import type { ConversationView } from "../../types/frontend";

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

  test("空历史也使用显式 session/thread owner，轮次入口不会继承旧会话输入", () => {
    let renderer: ReactTestRenderer;
    act(() => { renderer = create(<ChatPanel {...emptyPanelProps(() => undefined)} />); });
    const input = () => renderer!.root.findByProps({ "aria-label": "轮次 ID" });
    act(() => input().props.onChange({ target: { value: "turn-prior" } }));
    expect(input().props.value).toBe("turn-prior");
    act(() => renderer!.update(<ChatPanel {...emptyPanelProps(() => undefined)} threadId="thread-next" />));
    expect(input().props.value).toBe("");
    const shell = renderer!.root.findByProps({ className: "chat-stream-virtual-shell" });
    expect(shell.props["data-owner-session-id"]).toBe("session-long");
    expect(shell.props["data-owner-thread-id"]).toBe("thread-next");
    act(() => renderer!.unmount());
  });
});

describe("ChatPanel 正式轮次入口", () => {
  test("加载中可重复提交相同 ID 并取消旧请求，失败保留输入供原 ID 重试", async () => {
    const calls: Array<{ turnId: string; signal?: AbortSignal; reject: (error: Error) => void }> = [];
    const load = (turnId: string, signal?: AbortSignal) => new Promise<void>((_resolve, reject) => {
      calls.push({ turnId, signal, reject });
      signal?.addEventListener("abort", () => reject(new Error("请求已取消")), { once: true });
    });
    let renderer: ReactTestRenderer;
    act(() => { renderer = create(<ChatPanel {...emptyPanelProps(() => undefined)} historyError={null} onLoadAroundTurn={load} />); });
    const input = () => renderer!.root.findByProps({ "aria-label": "轮次 ID" });
    const form = () => renderer!.root.findByProps({ "aria-label": "跳到指定轮次" });
    const submit = () => form().props.onSubmit({ preventDefault() {} });
    act(() => { input().props.onChange({ target: { value: "turn-target" } }); });
    act(submit);
    expect(form().findByProps({ role: "status" }).children.join("")).toBe("正在读取指定轮次…");
    expect(input().props.disabled).toBe(false);
    expect(renderer!.root.findByProps({ type: "submit" }).props.disabled).toBe(false);
    act(submit);
    expect(calls.map((call) => call.turnId)).toEqual(["turn-target", "turn-target"]);
    expect(calls[0].signal?.aborted).toBe(true);
    await act(async () => { calls[1].reject(new Error("历史索引损坏")); });
    expect(form().findByProps({ role: "alert" }).children.join("")).toContain("历史索引损坏");
    expect(input().props.value).toBe("turn-target");
    act(submit);
    expect(calls).toHaveLength(3);
    expect(form().findAllByProps({ role: "alert" })).toHaveLength(0);
    act(() => renderer!.unmount());
  });
});
