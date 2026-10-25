import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import ChatTurn, { areChatTurnPropsEqual, type ChatTurnProps } from "./ChatTurn";
import { chatTurnProps, conversation } from "./chatTurnTestFixtures";

describe("ChatTurn memo 比较", () => {
  test("历史回答点击携带展示 owner 与快照，同 Turn revision 更新绑定后不复用旧回调", () => {
    const value = conversation("done");
    value.turnId = "turn-visible-final";
    value.turnRevision = 1;
    value.threadId = "thread-display";
    value.sourceViewId = "view-display";
    value.sourceViewRevision = 8;
    value.userMessage = null;
    value.responseParts![0].source = {
      message_sequence: 2,
      item_id: "item-visible-final",
      logical_item_ordinal: 12,
    };
    const clicks: Parameters<NonNullable<ChatTurnProps["onForkConversation"]>>[0][] = [];
    const onForkConversation: NonNullable<ChatTurnProps["onForkConversation"]> = (selection) => {
      clicks.push(selection);
    };
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<ChatTurn {...chatTurnProps(value)} onForkConversation={onForkConversation} />);
    });
    expect(renderer!.root.findAllByProps({ className: "chat-user-row" })).toHaveLength(0);
    const responseActions = renderer!.root.findByProps({ className: "chat-response-actions" });
    expect(responseActions.findAllByType("button")[0]?.props["data-fork-item-id"])
      .toBe("item-visible-final");
    act(() => {
      renderer!.root.findByProps({ "data-fork-item-id": "item-visible-final" }).props.onClick();
    });
    const next = { ...value, sourceViewId: "view-next", sourceViewRevision: 9 };
    act(() => {
      renderer!.update(<ChatTurn {...chatTurnProps(next)} onForkConversation={onForkConversation} />);
    });
    act(() => {
      renderer!.root.findByProps({ "data-fork-item-id": "item-visible-final" }).props.onClick();
    });
    expect(clicks).toEqual([
      { workspaceId: "gw_test", sessionId: "ses_web_replay", threadId: "thread-display",
        sourceViewId: "view-display", sourceViewRevision: 8, itemId: "item-visible-final" },
      { workspaceId: "gw_test", sessionId: "ses_web_replay", threadId: "thread-display",
        sourceViewId: "view-next", sourceViewRevision: 9, itemId: "item-visible-final" },
    ]);
    act(() => renderer!.unmount());
  });

  test("其他 Turn revision 更新时已完成 Turn 不重新渲染", () => {
    let renderCount = 0;
    const MemoProbe = React.memo(
      ({ value }: { value: ChatTurnProps }) => {
        renderCount += 1;
        return <span>{value.conversation.conversationId}</span>;
      },
      (previous, next) => areChatTurnPropsEqual(previous.value, next.value),
    );
    const first = conversation("done");
    first.turnId = "job_1";
    first.turnRevision = 4;
    first.turnItemsView = "full";
    let renderer: ReactTestRenderer;

    act(() => {
      renderer = create(<MemoProbe value={chatTurnProps(first)} />);
    });
    const reconstructed = conversation("done");
    reconstructed.turnId = "job_1";
    reconstructed.turnRevision = 4;
    reconstructed.turnItemsView = "full";
    act(() => {
      renderer!.update(<MemoProbe value={chatTurnProps(reconstructed)} />);
    });
    expect(renderCount).toBe(1);

    const revised = { ...reconstructed, turnRevision: 5 };
    act(() => {
      renderer!.update(<MemoProbe value={chatTurnProps(revised)} />);
    });
    expect(renderCount).toBe(2);
    renderer!.unmount();
  });

  test("活动 Turn 同 revision 的 streaming 更新不会被 memo 阻挡", () => {
    const previous = conversation("running", "job_started");
    previous.turnId = "job_1";
    previous.turnRevision = 2;
    previous.turnItemsView = "full";
    const next = {
      ...previous,
      events: [
        ...previous.events,
        { ...previous.events[0], event_id: "evt_2" },
      ],
    };

    expect(areChatTurnPropsEqual(
      chatTurnProps(previous),
      chatTurnProps(next),
    )).toBe(false);
  });

  test("同 revision 的工具详情 response parts 更新不会被 memo 阻挡", () => {
    const previous = conversation("done");
    previous.turnId = "job_1";
    previous.turnRevision = 2;
    previous.turnItemsView = "full";
    const next = {
      ...previous,
      responseParts: [
        ...previous.responseParts!,
        {
          part_id: "tool-call:call-1",
          kind: "tool_call" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 1, call_index: 0 },
          tool_call_id: "call-1",
          tool_name: "inspect_fixture",
          arguments: '{"path":"fixture.json"}',
        },
      ],
    };

    expect(areChatTurnPropsEqual(
      chatTurnProps(previous),
      chatTurnProps(next),
    )).toBe(false);
  });
});
