import { describe, expect, mock, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import ChatTurn from "./turn/ChatTurn";
import { chatTurnProps, conversation } from "./turn/chatTurnTestFixtures";
import { ChatItemMenuContext, type ChatItemMenuRequest } from "./itemContextMenu";

function menuEvent(selectedText = "") {
  const start = {}, end = {}, root = { host: {} };
  const source = { closest: () => null, contains: (node: object) => node === start || node === end,
    getRootNode: () => root,
    ownerDocument: { getSelection: () => selectedText ? {
      anchorNode: root.host, focusNode: root.host, toString: () => selectedText,
      getComposedRanges: ({ shadowRoots }: { shadowRoots: object[] }) => shadowRoots[0] === root
        ? [{ startContainer: start, endContainer: end }] : [],
    } : null },
  };
  return { key: "ContextMenu", target: source, currentTarget: source, clientX: 20, clientY: 30,
    preventDefault: mock(() => {}), stopPropagation: mock(() => {}) };
}

describe("Item 原生菜单接线", () => {
  test("工具卡片从合并结果末尾分支，携带所属历史快照且没有独立 fork 按钮", async () => {
    const value = conversation("done");
    Object.assign(value, { turnId: "turn-tools", threadId: "thread-tools", sourceViewId: "view-tools", sourceViewRevision: 17 });
    value.responseParts = [
      { part_id: "call", kind: "tool_call", projection: "detail", status: "completed", tool_name: "inspect",
        tool_call_id: "tool-1", arguments: '{"path":"a.ts"}', source: { message_sequence: 1, item_id: "item-call" } },
      { part_id: "result", kind: "tool_result", projection: "detail", status: "completed", tool_name: "inspect",
        tool_call_id: "tool-1", result: "完整工具结果", source: { message_sequence: 2, item_id: "item-result" } },
      ...value.responseParts!,
    ];
    const fork = mock(() => {});
    let menu: ChatItemMenuRequest | undefined;
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<ChatItemMenuContext.Provider value={request => { menu = request; }}>
      <ChatTurn {...chatTurnProps(value)} onForkConversation={fork} />
    </ChatItemMenuContext.Provider>); });
    try {
      await act(async () => { await renderer.root.findByProps({ className: "chat-thinking-toggle" }).props.onClick(); });
      const card = renderer.root.findByProps({ "data-chat-item-id": "call" });
      card.props.onContextMenu(menuEvent());
      expect(menu!.actions.map(action => action.id)).toEqual(["copyItem", "copyArguments", "copyResult", "forkItem"]);
      const action = menu!.actions.find(action => action.id === "forkItem")!;
      expect(action.enabled).toBe(true);
      action.run();
      expect(fork).toHaveBeenCalledWith({ workspaceId: "gw_test", sessionId: value.sessionId,
        threadId: "thread-tools", sourceViewId: "view-tools", sourceViewRevision: 17, itemId: "item-result" });
      expect(renderer.root.findAll(node => node.type === "button" && node.props["data-fork-item-id"])).toHaveLength(0);
    } finally { act(() => renderer.unmount()); }
  });

  test("卡片末尾缺少稳定身份时不退回到调用 Item 创建分支", async () => {
    const { responseItemMenuActions } = await import("./itemContextMenu");
    const actions = responseItemMenuActions({ kind: "aggregated_tool", id: "tool-card", toolName: "inspect",
      toolCallId: "tool-1", inputText: "{}", resultText: "结果", detailsLoaded: true, active: false,
      timestamp: null, rawStart: {}, rawEnd: {},
      responseSources: [
        { part_id: "call", kind: "tool_call", source: { message_sequence: 1, item_id: "call" } },
        { part_id: "result", kind: "tool_result", source: { message_sequence: 2 } },
      ],
    }, () => {});
    expect(actions.find(action => action.id === "forkItem")!.enabled).toBe(false);
  });

  test("右键只打开菜单，原始消息按显式操作加载；忙碌时编辑操作禁用", async () => {
    const value = conversation("done");
    value.userMessage = { ...value.userMessage!, content: "", metadata: { internal: true } };
    const load = mock(async () => "原始内部内容");
    let menu: ChatItemMenuRequest | undefined;
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<ChatItemMenuContext.Provider value={request => { menu = request; }}>
      <ChatTurn {...chatTurnProps(value)} onLoadAgentStateMessageRawContent={load} />
    </ChatItemMenuContext.Provider>); });
    try {
      const user = renderer.root.findByProps({ "data-chat-item-id": "msg_user" });
      user.props.onKeyDown(menuEvent());
      expect(load).not.toHaveBeenCalled();
      expect(menu!.actions.find(action => action.id === "rawContent")).toBeDefined();
      await act(async () => { await menu!.actions.find(action => action.id === "rawContent")!.run(); });
      expect(load).toHaveBeenCalledTimes(1);
      expect(renderer.root.findByProps({ className: "chat-user-raw-content" }).children).toEqual(["原始内部内容"]);
      value.userMessage = { ...value.userMessage!, content: "普通用户消息", metadata: {} };
      await act(async () => renderer.update(<ChatItemMenuContext.Provider value={request => { menu = request; }}>
        <ChatTurn {...chatTurnProps(value)} sessionBusy />
      </ChatItemMenuContext.Provider>));
      renderer.root.findByProps({ "data-chat-item-id": "msg_user" }).props.onKeyDown(menuEvent());
      expect(menu!.actions.find(action => action.id === "editAndContinue")!.enabled).toBe(false);
    } finally { act(() => renderer.unmount()); }
  });

  test("复制选区与完整源 Markdown 分开，链接右键不被截获", async () => {
    const previous = Object.getOwnPropertyDescriptor(globalThis, "navigator");
    const writeText = mock(async (_text: string) => {});
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText } } });
    const value = conversation("done");
    const text = "**完整 Markdown**\n\n另一段正文";
    value.responseParts![0].text = text;
    value.assistantMessages = [];
    let menu: ChatItemMenuRequest | undefined;
    const showMenu = mock((request: ChatItemMenuRequest) => { menu = request; });
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<ChatItemMenuContext.Provider value={showMenu}>
      <ChatTurn {...chatTurnProps(value)} />
    </ChatItemMenuContext.Provider>); });
    try {
      const response = renderer.root.findByProps({ "aria-label": "模型回复" });
      response.props.onContextMenu(menuEvent("选中文字"));
      await menu!.actions.find(action => action.id === "copySelection")!.run();
      expect(writeText).toHaveBeenLastCalledWith("选中文字");
      await menu!.actions.find(action => action.id === "copyItem")!.run();
      expect(writeText).toHaveBeenLastCalledWith(text);
      const linkEvent = { ...menuEvent(), target: { closest: () => ({ tagName: "A" }) } };
      response.props.onContextMenu(linkEvent);
      expect(showMenu).toHaveBeenCalledTimes(1);
      expect(linkEvent.preventDefault).not.toHaveBeenCalled();
    } finally {
      act(() => renderer.unmount());
      if (previous) Object.defineProperty(globalThis, "navigator", previous);
      else Reflect.deleteProperty(globalThis, "navigator");
    }
  });
});
