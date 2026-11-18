import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { expect, test, mock } from "bun:test";
import ContextNotice from "./ContextNotice";
import { ChatItemContentContext, ChatItemOwnerContext, type ChatItemContentHost } from "./itemContextMenu";
import type { TurnResponsePart } from "../../types/backend";
import { responsePartsToTimelineItems } from "../../state/timeline/responseParts";

const notice: TurnResponsePart = { kind: "context_notice", part_id: "notice:context", projection: "summary",
  source: { message_sequence: 0, item_id: "notice", logical_item_ordinal: 4 },
  context_kind: "checkpoint_reminder", context_model_call_id: "call-2", context_before_item_id: "answer-2" };

test("默认不读正文，展开才读取；折叠卸载正文，再展开重新读取", async () => {
  const read = mock<ChatItemContentHost["read"]>(async () => [{ name: "context", language: "plaintext", content: "<system_reminder>保留标签</system_reminder>", encoding: "utf-8" }]);
  const host: ChatItemContentHost = { read, open: async () => {} };
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<ChatItemContentContext.Provider value={host}>
    <ChatItemOwnerContext.Provider value={{ sessionId: "session", threadId: "thread" }}>
      <ContextNotice notices={[notice]} />
    </ChatItemOwnerContext.Provider>
  </ChatItemContentContext.Provider>); });
  try {
    expect(read).not.toHaveBeenCalled();
    const marker = () => renderer.root.findByProps({ className: "chat-context-marker" });
    expect(marker().props["aria-expanded"]).toBe(false);
    await act(async () => marker().props.onClick());
    expect(read).toHaveBeenCalledTimes(1);
    expect(read.mock.calls[0].slice(0, 2)).toEqual([{ sessionId: "session", threadId: "thread" }, "notice"]);
    expect(renderer.root.findByProps({ className: "chat-context-details" })).toBeTruthy();
    await act(async () => renderer.root.findByProps({ className: "chat-context-toggle" }).props.onClick());
    expect(renderer.root.findAllByProps({ className: "chat-context-details" })).toHaveLength(0);
    await act(async () => marker().props.onClick());
    expect(read).toHaveBeenCalledTimes(2);
  } finally { act(() => renderer.unmount()); }
});

test("多条提示先展示标题，逐项展开才读取；整组收起后恢复默认折叠", async () => {
  const read = mock<ChatItemContentHost["read"]>(async (_owner, itemId) => [
    { name: "context", language: "plaintext", content: `${itemId} 正文`, encoding: "utf-8" },
  ]);
  const host: ChatItemContentHost = { read, open: async () => {} };
  const notices = [notice, { ...notice, part_id: "second:context", source: { ...notice.source, item_id: "second" } }];
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<ChatItemContentContext.Provider value={host}>
    <ChatItemOwnerContext.Provider value={{ sessionId: "session", threadId: "thread" }}>
      <ContextNotice notices={notices} />
    </ChatItemOwnerContext.Provider>
  </ChatItemContentContext.Provider>); });
  try {
    await act(async () => renderer.root.findByProps({ className: "chat-context-marker" }).props.onClick());
    const sources = () => renderer.root.findAllByProps({ className: "chat-context-source-toggle" });
    expect(sources().map(source => source.props["aria-expanded"])).toEqual([false, false]);
    expect(read).not.toHaveBeenCalled();
    await act(async () => sources()[0].props.onClick());
    expect(read.mock.calls.map(call => call[1])).toEqual(["notice"]);
    expect(sources().map(source => source.props["aria-expanded"])).toEqual([true, false]);
    await act(async () => sources()[0].props.onClick());
    expect(read.mock.calls[0][2]?.aborted).toBe(true);
    expect(renderer.root.findAllByProps({ className: "chat-context-source-body" })).toHaveLength(0);
    await act(async () => sources()[1].props.onClick());
    expect(read.mock.calls.map(call => call[1])).toEqual(["notice", "second"]);
    await act(async () => renderer.root.findByProps({ className: "chat-context-link chat-context-collapse" }).props.onClick());
    expect(renderer.root.findAllByProps({ className: "chat-context-details" })).toHaveLength(0);
    await act(async () => renderer.root.findByProps({ className: "chat-context-marker" }).props.onClick());
    expect(sources().map(source => source.props["aria-expanded"])).toEqual([false, false]);
    expect(read).toHaveBeenCalledTimes(2);
  } finally { act(() => renderer.unmount()); }
});

test("长提示仅保留有限预览，全文交给宿主编辑器，失败在当前详情中显示", async () => {
  const full = Array.from({ length: 100 }, (_, index) => `第 ${index + 1} 行 ${"原文".repeat(100)}`).join("\n") + "\nFULL_TAIL";
  const read = mock<ChatItemContentHost["read"]>(async () => [{ name: "context", language: "plaintext", content: full, encoding: "utf-8" }]);
  const open = mock<ChatItemContentHost["open"]>(async () => { throw new Error("编辑器已关闭"); });
  const host: ChatItemContentHost = { read, open };
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<ChatItemContentContext.Provider value={host}>
    <ChatItemOwnerContext.Provider value={{ sessionId: "session", threadId: "thread" }}>
      <ContextNotice notices={[notice]} />
    </ChatItemOwnerContext.Provider>
  </ChatItemContentContext.Provider>); });
  try {
    await act(async () => renderer.root.findByProps({ className: "chat-context-marker" }).props.onClick());
    const preview = renderer.root.findByProps({ className: "chat-context-preview" }).children.join("");
    expect(preview.length).toBeLessThanOrEqual(2400);
    expect(preview.split("\n").length).toBeLessThanOrEqual(12);
    expect(preview).not.toContain("FULL_TAIL");
    await act(async () => renderer.root.findByProps({ className: "chat-context-link chat-context-open" }).props.onClick());
    expect(open.mock.calls[0]).toEqual([{ sessionId: "session", threadId: "thread" }, "notice"]);
    expect(renderer.root.findByProps({ role: "alert" }).children.join("")).toContain("编辑器已关闭");
    expect(renderer.root.findByProps({ className: "chat-context-preview" }).children.join("")).toBe(preview);
  } finally { act(() => renderer.unmount()); }
});

test("展开未完成时收起会取消读取，迟到正文不重新挂载", async () => {
  let complete!: (value: Awaited<ReturnType<ChatItemContentHost["read"]>>) => void;
  const read = mock<ChatItemContentHost["read"]>(() => new Promise(resolve => { complete = resolve; }));
  const host: ChatItemContentHost = { read, open: async () => {} };
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<ChatItemContentContext.Provider value={host}>
    <ChatItemOwnerContext.Provider value={{ sessionId: "session", threadId: "thread" }}>
      <ContextNotice notices={[notice]} />
    </ChatItemOwnerContext.Provider>
  </ChatItemContentContext.Provider>); });
  try {
    await act(async () => renderer.root.findByProps({ className: "chat-context-marker" }).props.onClick());
    expect(renderer.root.findByProps({ role: "status" })).toBeTruthy();
    await act(async () => renderer.root.findByProps({ className: "chat-context-toggle" }).props.onClick());
    expect(read.mock.calls[0][2]?.aborted).toBe(true);
    await act(async () => complete([{ name: "context", language: "plaintext", content: "迟到内容", encoding: "utf-8" }]));
    expect(renderer.root.findAllByProps({ className: "chat-context-source-body" })).toHaveLength(0);
  } finally { act(() => renderer.unmount()); }
});

test("同次注入合成一条标记，在历史和实时模型输出前展示", () => {
  const earlier: TurnResponsePart = { kind: "text", part_id: "answer-1", projection: "detail", text: "之前",
    source: { message_sequence: 1, item_id: "answer-1", logical_item_ordinal: 1, model_call_id: "call-1" } };
  for (const projection of ["detail", "streaming"] as const) {
    const answer: TurnResponsePart = { kind: "text", part_id: "answer-2", projection, text: "之后",
      source: { message_sequence: 2, ...(projection === "detail" ? { item_id: "answer-2" } : {}), model_call_id: "call-2" } };
    const items = responsePartsToTimelineItems([earlier, answer, notice, { ...notice, part_id: "second:context", source: { ...notice.source, item_id: "second" } }]);
    expect(items.map(item => item.kind)).toEqual(["aggregated_text", "context_event", "aggregated_text"]);
    expect(items[1]).toMatchObject({ notices: [notice, { ...notice, part_id: "second:context", source: { ...notice.source, item_id: "second" } }] });
  }
});
