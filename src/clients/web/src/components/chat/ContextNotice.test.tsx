import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { expect, test } from "bun:test";
import ContextNotice from "./ContextNotice";
import { ChatItemMenuContext, ChatItemOwnerContext,
  ContextNoticeContinuationContext,
  type ChatItemMenuRequest } from "./itemContextMenu";
import type { TurnResponsePart } from "../../types/backend";
import { responsePartsToTimelineItems } from "../../state/timeline/responseParts";

const notice = { kind: "context_notice", part_id: "notice:context", projection: "summary",
  source: { message_sequence: 0, item_id: "notice", logical_item_ordinal: 4 },
  context_kind: "checkpoint_reminder", context_model_call_id: "call-2", context_before_item_id: "answer-2",
  context_content_ref: { kind: "item", item_id: "notice" }, context_content_length: 512,
  context_anchor: "before_model_call", context_anchor_ordinal: 4 } satisfies TurnResponsePart;

test("展开来源、键盘菜单及取消只消费元数据，不读取正文", async () => {
  let request: ChatItemMenuRequest | undefined;
  let renderer!: ReactTestRenderer;
  const second = { ...notice, part_id: "second:context", context_content_ref: {
    kind: "sealed_contribution", session_id: "original-session", thread_id: "original-thread",
    assembly_id: "assembly", contribution_id: "prompt",
  } } satisfies TurnResponsePart;
  await act(async () => { renderer = create(<>
    <ChatItemOwnerContext.Provider value={{ sessionId: "session", threadId: "thread" }}>
      <ChatItemMenuContext.Provider value={value => { request = value; }}>
        <ContextNotice notices={[notice, second]} />
      </ChatItemMenuContext.Provider>
    </ChatItemOwnerContext.Provider>
  </>); });
  try {
    const marker = renderer.root.findByProps({ className: "chat-context-marker" });
    await act(async () => marker.props.onClick());
    expect(renderer.root.findAllByProps({ className: "chat-context-source" })).toHaveLength(2);
    expect(renderer.root.findAllByType("pre")).toHaveLength(0);
    const element = { ownerDocument: { getSelection: () => null }, getRootNode: () => ({}), contains: () => true };
    marker.props.onKeyDown({ key: "F10", shiftKey: true, target: { closest: () => null }, currentTarget: element,
      preventDefault() {}, stopPropagation() {} });
    expect(request?.target?.contextSources?.map(source => source.reference)).toEqual([notice.context_content_ref, second.context_content_ref]);
    expect(request?.actions.map(action => action.id)).toEqual(["toggleContext"]);
    request = undefined;
    await act(async () => marker.props.onClick());
    expect(renderer.root.findAllByProps({ className: "chat-context-source" })).toHaveLength(0);
  } finally { act(() => renderer.unmount()); }
});

test("同请求同锚点合成一条标记，在历史和实时模型输出前展示", () => {
  const earlier: TurnResponsePart = { kind: "text", part_id: "answer-1", projection: "detail", text: "之前",
    source: { message_sequence: 1, item_id: "answer-1", logical_item_ordinal: 1, model_call_id: "call-1" } };
  for (const projection of ["detail", "streaming"] as const) {
    const answer: TurnResponsePart = { kind: "text", part_id: "answer-2", projection, text: "之后",
      source: { message_sequence: 2, ...(projection === "detail" ? { item_id: "answer-2" } : {}), model_call_id: "call-2" } };
    const items = responsePartsToTimelineItems([earlier, answer, notice, { ...notice, part_id: "second:context" }]);
    expect(items.map(item => item.kind)).toEqual(["aggregated_text", "context_event", "aggregated_text"]);
  }
  const failed = responsePartsToTimelineItems([earlier, { ...notice, context_before_item_id: undefined, context_anchor_ordinal: 2 }]);
  expect(failed.map(item => item.kind)).toEqual(["aggregated_text", "context_event"]);
  const beforeUser = responsePartsToTimelineItems([earlier, { ...notice, context_anchor: "before_user_message" }]);
  expect(beforeUser.map(item => item.kind)).toEqual(["aggregated_text"]);
});

test("元数据续页只由显式按钮触发，加载中禁用并展示失败", async () => {
  const calls: string[] = [];
  let reject!: (error: Error) => void;
  const pending = new Promise<void>((_, no) => { reject = no; });
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<>
    <ContextNoticeContinuationContext.Provider value={cursor => { calls.push(cursor); return pending; }}>
      <ContextNotice notices={[{ ...notice, context_cursor: "original-page-remainder" }]} />
    </ContextNoticeContinuationContext.Provider>
  </>); });
  try {
    expect(calls).toEqual([]);
    const button = renderer.root.findAllByType("button").at(-1)!;
    let request!: Promise<void>;
    await act(async () => { request = button.props.onClick(); });
    expect(calls).toEqual(["original-page-remainder"]);
    expect(button.props.disabled).toBe(true);
    await act(async () => { reject(new Error("原历史视图已变化")); await request; });
    expect(renderer.root.findByProps({ role: "alert" }).children).toEqual(["原历史视图已变化"]);
    expect(button.props.disabled).toBe(false);
  } finally { act(() => renderer.unmount()); }
});
