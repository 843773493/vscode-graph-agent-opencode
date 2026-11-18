import { describe, expect, mock, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import ChatHistoryPageHeader from "./ChatHistoryPageHeader";

describe("ChatHistoryPageHeader", () => {
  test("历史页头保持同一容器，加载只切换不占布局的进度图标", () => {
    const props = { loadingOlderMessages: false, error: null, onRetry: () => undefined };
    const renderer = create(<ChatHistoryPageHeader {...props} />);
    const header = renderer.root.findByProps({ className: "chat-history-page-header" });
    expect(header.children).toHaveLength(0);
    act(() => { renderer.update(<ChatHistoryPageHeader {...props} loadingOlderMessages />); });
    expect(renderer.root.findByProps({ className: "chat-history-page-header" })).toBe(header);
    expect(renderer.root.findByProps({ role: "status" }).props["aria-label"]).toBe("正在加载更早消息…");
    act(() => { renderer.update(<ChatHistoryPageHeader {...props} />); });
    expect(header.children).toHaveLength(0);
    renderer.unmount();
  });

  test("失败保留原始错误和重试入口，不叠加普通进度文案", () => {
    const retry = mock(() => undefined);
    const renderer = create(<ChatHistoryPageHeader loadingOlderMessages error="同步失败：HTTP 503" onRetry={retry} />);
    expect(renderer.root.findByProps({ role: "alert" }).props.title).toBe("同步失败：HTTP 503");
    expect(renderer.root.findAllByProps({ role: "status" })).toHaveLength(0);
    act(() => { renderer.root.findByType("button").props.onClick(); });
    expect(retry).toHaveBeenCalledTimes(1);
    renderer.unmount();
  });
});
