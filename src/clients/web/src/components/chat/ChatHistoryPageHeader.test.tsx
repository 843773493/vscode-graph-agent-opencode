import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import ChatHistoryPageHeader from "./ChatHistoryPageHeader";

describe("ChatHistoryPageHeader", () => {
  test("partial 页头不误报已到达会话起点", () => {
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ChatHistoryPageHeader
          projectionState="partial"
          hasOlderMessages={false}
          loadingOlderMessages={false}
          error={null}
          onRetry={() => undefined}
        />,
      );
    });

    expect(renderer!.root.findAll(
      (node) => node.children.includes("旧 Turn 正在迁移，完成后可继续向上加载"),
    )).toHaveLength(1);
    expect(renderer!.root.findAll(
      (node) => node.children.includes("已到达会话起点"),
    )).toHaveLength(0);
    renderer!.unmount();
  });
});
