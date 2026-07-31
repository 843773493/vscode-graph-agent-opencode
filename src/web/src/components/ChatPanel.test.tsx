import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import ChatPanel from "./ChatPanel";

function emptyPanelProps(onRetryHistory: () => void) {
  return {
    apiPort: 8014,
    workspaceId: "workspace-long",
    conversations: [],
    expandDetails: false,
    hasActiveSession: true,
    hasOlderMessages: false,
    loadingOlderMessages: false,
    historyLoading: false,
    projectionState: "ready" as const,
    timelineGeneration: 1,
    projectionEpoch: 1,
    historyError: "Turn 投影索引损坏：manifest ordinal 不连续",
    onLoadOlderMessages: async () => {},
    loadingDetailTurnIds: [],
    onLoadTurnDetails: async () => {},
    onRetryHistory,
    onReplayTurn: async () => {},
    onUpdatePending: async () => {},
    onRemovePending: async () => {},
    onClearPending: async () => {},
    onReorderPending: async () => {},
    onSendPendingImmediately: async () => {},
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
    renderer!.unmount();
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
    renderer!.unmount();
  });

});
