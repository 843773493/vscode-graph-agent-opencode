import { TranscriptTestView } from "../../../tests/fixtures/transcriptView";
import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import OriginalChatTurn, { areChatTurnPropsEqual, type ChatTurnProps } from "./ChatTurn";
import { chatTurnProps, conversation } from "./chatTurnTestFixtures";
import { ChatItemMenuContext, type ChatItemMenuRequest } from "../itemContextMenu";

describe("ChatTurn memo 比较", () => {
  test("同 revision 新增来源标记不会被复用挡住，标记位于真实用户消息前", () => {
    const previous = conversation("done");
    previous.turnId = "turn-context";
    previous.threadId = "thread-context";
    previous.turnRevision = 1;
    const next = { ...previous, responseParts: [...previous.responseParts!, {
      part_id: "context-first", kind: "context_notice" as const, projection: "summary" as const,
      source: { message_sequence: 0 }, context_model_call_id: "call-context",
      context_content_ref: { kind: "sealed_contribution", session_id: "session", thread_id: "thread",
        assembly_id: "assembly", contribution_id: "prompt" },
      context_anchor: "before_user_message", context_before_item_id: "root-context", context_anchor_ordinal: 0,
    }] };
    expect(areChatTurnPropsEqual(chatTurnProps(previous), chatTurnProps(next))).toBe(false);
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(<ChatTurn {...chatTurnProps(next)} />); });
    try {
      const nodes = renderer.root.findAll(node => typeof node.type === "string"
        && (node.props["data-context-marker"] || node.props.className === "chat-user-row"));
      expect(nodes.map(node => node.props["data-context-marker"] ?? "user")).toEqual(["context-first", "user"]);
      expect(renderer.root.findAllByProps({ "data-context-marker": "context-first" })).toHaveLength(1);
    } finally { act(() => renderer.unmount()); }
  });

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
    let menu: ChatItemMenuRequest | undefined;
    const render = (current: typeof value) => <ChatItemMenuContext.Provider value={request => { menu = request; }}>
      <ChatTurn {...chatTurnProps(current)} onForkConversation={onForkConversation} />
    </ChatItemMenuContext.Provider>;
    const forkFromResponse = () => {
      const source = { closest: () => null, getRootNode: () => ({}), ownerDocument: { getSelection: () => null } };
      renderer!.root.findByProps({ "aria-label": "模型回复" }).props.onKeyDown({
        key: "ContextMenu", target: source, currentTarget: source, preventDefault() {}, stopPropagation() {},
      });
      menu!.actions.find(action => action.id === "forkItem")!.run();
    };
    act(() => {
      renderer = create(render(value));
    });
    expect(renderer!.root.findAllByProps({ className: "chat-user-row" })).toHaveLength(0);
    expect(renderer!.root.findAllByProps({ "data-fork-item-id": "item-visible-final" })).toHaveLength(0);
    act(() => {
      forkFromResponse();
    });
    const next = { ...value, sourceViewId: "view-next", sourceViewRevision: 9 };
    act(() => {
      renderer!.update(render(next));
    });
    act(() => {
      forkFromResponse();
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

function ChatTurn(props: React.ComponentProps<typeof OriginalChatTurn>) { return <TranscriptTestView><OriginalChatTurn {...props} /></TranscriptTestView>; }
