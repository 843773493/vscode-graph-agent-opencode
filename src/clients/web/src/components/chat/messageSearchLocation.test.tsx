import { expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import { MessageSearchLocation, MessageSearchLocationContext, messageRangeOffsets } from "./messageSearchLocation";
import type { TurnMessageLocationDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";

test("码点位置在多行、emoji 和重复词原文中定位指定一次命中", () => {
  const content = "😀词\n前缀😀词\n尾";
  const [start, end] = messageRangeOffsets(content, 5, 7);
  expect(content.slice(start, end)).toBe("😀词");
  expect(start).toBe(6);
  expect(() => messageRangeOffsets(content, 0, 100)).toThrow("区间越界");
  expect(() => messageRangeOffsets(content, 3, 3)).toThrow("区间越界");
});

test("原文只显示在所属轮次，完整文本保持不变且可关闭", () => {
  const message: TurnMessageLocationDTO = {
    turn_id: "turn-found", role: "user", content: "😀词\n前缀😀词\n尾",
    reference: { item_id: "item-found", content_hash: "opaque", start_codepoint: 5, end_codepoint: 7 },
  };
  let closed = 0;
  let renderer!: ReturnType<typeof create>;
  act(() => { renderer = create(<MessageSearchLocationContext.Provider value={{ message, close: () => { closed += 1; } }}>
    <MessageSearchLocation turnId="other-turn" /><MessageSearchLocation turnId="turn-found" />
  </MessageSearchLocationContext.Provider>); });
  try {
    expect(renderer.root.findAllByType("section")).toHaveLength(1);
    const mark = renderer.root.findByType("mark");
    expect(mark.props["data-message-item-id"]).toBe("item-found");
    expect(mark.children).toEqual(["😀词"]);
    expect(renderer.root.findByType("pre").children.map(child => typeof child === "string" ? child : child.children.join("")).join("")).toBe(message.content);
    act(() => { renderer.root.findByType("button").props.onClick(); });
    expect(closed).toBe(1);
  } finally { act(() => renderer.unmount()); }
});
