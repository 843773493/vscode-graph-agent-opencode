import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { afterEach, describe, expect, test } from "bun:test";
import { Virtuoso, VirtuosoMockContext } from "react-virtuoso";
import ChatPanel from "./ChatPanel";
import type { ConversationView } from "../../types/frontend";

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
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      addEventListener: () => {},
      removeEventListener: () => {},
      getComputedStyle: () => ({ getPropertyValue: () => "" }),
      requestAnimationFrame: (handler: () => void) => globalThis.setTimeout(handler, 0),
      cancelAnimationFrame: (id: number) => globalThis.clearTimeout(id),
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

let originalWindowDescriptor: PropertyDescriptor | undefined;

afterEach(() => {
  // 真实 react-virtuoso 需要 window 才能走通尺寸测量；用完必须还原，
  // 否则这个桩会泄漏给同一进程后续所有测试文件（它们会误以为运行在浏览器里）。
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
  originalWindowDescriptor = undefined;
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
    onUpdatePending: async () => {},
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

    renderer!.unmount();
  });
});
