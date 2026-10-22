import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { afterEach, describe, expect, test } from "bun:test";
import { Virtuoso, VirtuosoMockContext } from "react-virtuoso";
import ChatPanel from "./ChatPanel";
import type { InternalDisplayProvenance, InternalExecutionDisplayPart } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

/** 真实 react-virtuoso 只在需要浏览器尺寸测量时才拒绝无 DOM 环境；
 * createNodeMock 提供宿主节点替身、VirtuosoMockContext 固定视口与条目高度，
 * 就能让真实实现直接渲染。这样测试观察到的就是 ChatPanel 真正传给 Virtuoso 的
 * props，不必再替换模块——bun 的 mock.module 是进程级且不可撤销的，会污染同一
 * 进程后续所有测试文件看到的 react-virtuoso。 */

interface VirtuosoHostNode {
  style: Record<string, string>;
  offsetHeight: number;
  offsetWidth: number;
}

function createVirtuosoHostNode(): unknown {
  return {
    style: new Proxy({} as Record<string, string>, { get: () => "", set: () => true }),
    offsetHeight: 600,
    offsetWidth: 800,
    scrollHeight: 600,
    scrollWidth: 800,
    scrollTop: 0,
    clientHeight: 600,
    clientWidth: 800,
    addEventListener: () => {},
    removeEventListener: () => {},
    appendChild: () => {},
    removeChild: () => {},
    setAttribute: () => {},
    removeAttribute: () => {},
    querySelector: () => null,
    getBoundingClientRect: () => ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 800,
      bottom: 600,
      width: 800,
      height: 600,
      toJSON: () => undefined,
    }),
  };
}

function installVirtuosoEnvironment(): void {
  originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  originalRequestFrameDescriptor = Object.getOwnPropertyDescriptor(globalThis, "requestAnimationFrame");
  originalCancelFrameDescriptor = Object.getOwnPropertyDescriptor(globalThis, "cancelAnimationFrame");
  const requestFrame = (handler: FrameRequestCallback) => {
    const timer = globalThis.setTimeout(() => { frameTimers.delete(timer); handler(0); }, 0);
    frameTimers.add(timer);
    return timer;
  };
  const cancelFrame = (id: ReturnType<typeof globalThis.setTimeout>) => {
    frameTimers.delete(id);
    globalThis.clearTimeout(id);
  };
  Object.defineProperty(globalThis, "requestAnimationFrame", { configurable: true, value: requestFrame });
  Object.defineProperty(globalThis, "cancelAnimationFrame", { configurable: true, value: cancelFrame });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      addEventListener: () => {},
      removeEventListener: () => {},
      getComputedStyle: () => ({ getPropertyValue: () => "" }),
      requestAnimationFrame: requestFrame,
      cancelAnimationFrame: cancelFrame,
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

let originalWindowDescriptor: PropertyDescriptor | undefined;
let originalRequestFrameDescriptor: PropertyDescriptor | undefined;
let originalCancelFrameDescriptor: PropertyDescriptor | undefined;
const frameTimers = new Set<ReturnType<typeof globalThis.setTimeout>>();

afterEach(() => {
  // 真实 react-virtuoso 需要 window 才能走通尺寸测量；用完必须还原，
  // 否则这个桩会泄漏给同一进程后续所有测试文件（它们会误以为运行在浏览器里）。
  for (const timer of frameTimers) globalThis.clearTimeout(timer);
  frameTimers.clear();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("requestAnimationFrame", originalRequestFrameDescriptor);
  restoreGlobalDescriptor("cancelAnimationFrame", originalCancelFrameDescriptor);
  originalWindowDescriptor = undefined;
  originalRequestFrameDescriptor = undefined;
  originalCancelFrameDescriptor = undefined;
});

function withVirtuosoMock(children: React.ReactElement): React.ReactElement {
  return (
    <VirtuosoMockContext.Provider value={{ viewportHeight: 300, itemHeight: 30 }}>
      {children}
    </VirtuosoMockContext.Provider>
  );
}

type ChatPanelProps = Parameters<typeof ChatPanel>[0];

function panelProps(
  conversations: ConversationView[],
  overrides: Partial<ChatPanelProps> = {},
): ChatPanelProps {
  return {
    apiPort: 8014,
    workspaceId: "workspace-test",
    sessionId: "session-1",
    threadId: "thread-main",
    conversations,
    expandDetails: false,
    hasActiveSession: true,
    hasNewerMessages: false,
    hasOlderMessages: false,
    loadingNewerMessages: false,
    loadingOlderMessages: false,
    historyLoading: false,
    projectionState: "ready",
    historyError: null,
    onLoadAroundTurn: async () => {},
    onLoadNewerMessages: async () => {},
    onLoadOlderMessages: async () => {},
    onLoadTurnDetails: async () => {},
    onLoadAgentStateMessageRawContent: async () => "",
    onRetryHistory: () => {},
    onReplayTurn: async () => {},
    onRemovePending: async () => {},
    onChangePendingPolicy: async () => {},
    ...overrides,
  };
}

function conversation(text: string): ConversationView {
  return {
    conversationId: "turn-1",
    displayMode: "live",
    sessionId: "session-1",
    userMessage: null,
    thinkingBlocks: text ? [{ kind: "reasoning", text }] : [],
    events: [],
    status: "running",
    jobId: "job-1",
    pending: false,
    source: "turn",
  } as unknown as ConversationView;
}

/** 直接读出 ChatPanel 交给真实 Virtuoso 的 props。 */
function virtuosoProps(renderer: ReactTestRenderer): Record<string, unknown> {
  return renderer.root.findByType(Virtuoso).props as Record<string, unknown>;
}

describe("ChatPanel Virtuoso 渲染引用", () => {
  test("轮次候选展示序号和用户正文摘要，仅接纳已提交历史 Turn", () => {
    installVirtuosoEnvironment();
    const history: ConversationView = {
      ...conversation(""), conversationId: "turn-history", turnId: "turn-history", displayMode: "history", status: "done",
      turnOrdinal: 9, rootLogicalItemOrdinal: 10, turnItemsView: "summary",
      userMessage: { message_id: "message-history", session_id: "session-1", thread_id: "thread-main", role: "user",
        content: "  真实  用户\n摘要  ", attachments: [], metadata: {}, created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z" },
    };
    const live = { ...conversation(""), conversationId: "turn-live" };
    const pending = { ...conversation(""), conversationId: "pending", turnId: "pending", pending: true };
    const pendingActiveJobOverlay = {
      ...conversation(""),
      conversationId: "pending-overlay",
      pending: true,
      activeJobOverlay: true,
    };
    let renderer: ReactTestRenderer;
    act(() => { renderer = create(withVirtuosoMock(<ChatPanel {...panelProps([
      history,
      live,
      pending,
      pendingActiveJobOverlay,
    ])} />), {
      createNodeMock: createVirtuosoHostNode,
    }); });
    const options = renderer!.root.findAllByType("option");
    expect(options).toHaveLength(1);
    expect(options[0].props.value).toBe("turn-history");
    expect(options[0].children.join("")).toBe("第 9 轮 · 真实 用户 摘要");
    const virtuoso = virtuosoProps(renderer!);
    const items = virtuoso.data as Array<{ key: string; conversation: ConversationView }>;
    const computeItemKey = virtuoso.computeItemKey as (
      index: number,
      item: { key: string; conversation: ConversationView },
      context: unknown,
    ) => string;
    expect(items.map((item, index) => computeItemKey(index, item, virtuoso.context))).toEqual([
      "turn-history",
      "turn-live",
      "pending-overlay",
    ]);
    const activeOverlay = items.find((item) => item.conversation.conversationId === "pending-overlay");
    expect(activeOverlay?.conversation.pending).toBe(true);
    expect(activeOverlay?.conversation.activeJobOverlay).toBe(true);
    expect(activeOverlay?.conversation.turnId).toBeUndefined();
    const renderItem = virtuoso.itemContent as (
      index: number,
      item: { key: string; conversation: ConversationView },
      context: unknown,
    ) => React.ReactNode;
    const renderedMarkers = items.map((item, index) => {
      const rendered = renderItem(index, item, virtuoso.context);
      return React.isValidElement(rendered)
        ? rendered.props["data-turn-id"]
        : undefined;
    });
    expect(renderedMarkers).toEqual(["turn-history", undefined, undefined]);
    act(() => renderer!.unmount());
  });

  test("流式更新和取消后重试保持回调引用，同时读取最新历史状态", () => {
    installVirtuosoEnvironment();
    const first = panelProps([conversation("第一段")]);
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(withVirtuosoMock(<ChatPanel {...first} />), {
        createNodeMock: createVirtuosoHostNode,
      }) as unknown as ReactTestRenderer;
    });
    const initialProps = virtuosoProps(renderer!);

    act(() => {
      renderer!.update(
        withVirtuosoMock(
          <ChatPanel
            {...first}
            conversations={[conversation("第一段继续流式增长")]}
          />,
        ),
      );
    });
    const streamingProps = virtuosoProps(renderer!);
    expect(streamingProps.components).toBe(initialProps.components);
    expect(streamingProps.itemContent).toBe(initialProps.itemContent);
    expect(streamingProps.computeItemKey).toBe(initialProps.computeItemKey);
    expect(streamingProps.context).toBe(initialProps.context);

    let retryCount = 0;
    const retry = () => {
      retryCount += 1;
    };
    act(() => {
      renderer!.update(
        withVirtuosoMock(
          <ChatPanel
            {...first}
            conversations={[conversation("取消后重试的流式正文")]}
            hasOlderMessages
            loadingOlderMessages
            historyError="历史加载仍在进行"
            onRetryHistory={retry}
          />,
        ),
      );
    });
    const retryProps = virtuosoProps(renderer!);

    expect(retryProps.components).toBe(initialProps.components);
    expect(retryProps.itemContent).toBe(initialProps.itemContent);
    expect(retryProps.computeItemKey).toBe(initialProps.computeItemKey);
    expect(retryProps.context).not.toBe(streamingProps.context);

    expect(renderer!.root.findAllByProps({ role: "alert" })).toHaveLength(1);
    expect(renderer!.root.findAll(
      (node) => node.children.includes("正在加载更早消息…"),
    )).toHaveLength(1);
    const retryButton = renderer!.root.findByProps({ children: "重试" });
    retryButton.props.onClick();
    expect(retryCount).toBe(1);

    act(() => renderer!.unmount());
  });
});

  test("汇报入口只回传当前 Session 精确 child/execution 来源，DOM 不暴露来源身份", () => {
    installVirtuosoEnvironment();
    const source: InternalDisplayProvenance = {
      source_session_id: "session-1", source_thread_id: "child-private", source_execution_id: "execution-private",
    };
    const display: InternalExecutionDisplayPart = {
      kind: "generated_session_result", text: "子线程已提交报告。", item_id: "notice-private", item_sequence: 1,
      logical_item_ordinal: 0, created_at: "2026-10-22T00:00:00Z", provenance: source,
    };
    const opened: InternalDisplayProvenance[] = [];
    let renderer!: ReactTestRenderer;
    act(() => { renderer = create(withVirtuosoMock(<ChatPanel {...panelProps([])}
      internalExecutions={[{ execution_id: "target-execution", job_id: "job-report", session_id: "session-1",
        thread_id: "thread-main", status: "completed", created_at: display.created_at, updated_at: display.created_at,
        display_parts: [display], response_parts: [] }]}
      onOpenChildReport={(target) => opened.push(target)} />), { createNodeMock: createVirtuosoHostNode }); });
    const props = renderer.root.findByType(Virtuoso).props;
    const renderItem = (provenance: InternalDisplayProvenance) => props.itemContent(0, {
      kind: "internal_execution", key: "report", executionId: "target-execution",
      displayPart: { ...display, provenance },
    }, props.context) as React.ReactElement;
    let item!: ReactTestRenderer;
    act(() => { item = create(renderItem(source)); });
    const button = item.root.findByProps({ children: "查看来源报告" });
    act(() => button.props.onClick());
    expect(opened).toEqual([source]);
    const markup = JSON.stringify(item.toJSON());
    for (const id of Object.values(source)) expect(markup).not.toContain(id);
    act(() => item.unmount());
    for (const invalid of [
      { ...source, source_session_id: "copied-source-session" },
      { ...source, source_thread_id: "thread-main" },
      { ...source, source_thread_id: undefined },
      { ...source, source_execution_id: undefined },
    ]) {
      act(() => { item = create(renderItem(invalid)); });
      expect(item.root.findAllByType("button")).toHaveLength(0);
      act(() => item.unmount());
    }
    act(() => renderer.unmount());
  });
