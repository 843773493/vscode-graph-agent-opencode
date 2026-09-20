import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { describe, expect, mock, test } from "bun:test";
import type { ConversationView } from "../types/frontend";

interface MockVirtuosoProps {
  context?: {
    stateRef: React.MutableRefObject<unknown>;
  };
  components?: {
    Header?: (props: { context: NonNullable<MockVirtuosoProps["context"]> }) => React.ReactNode;
    Footer?: (props: { context: NonNullable<MockVirtuosoProps["context"]> }) => React.ReactNode;
  };
  itemContent?: (...args: unknown[]) => React.ReactNode;
  computeItemKey?: (...args: unknown[]) => React.Key;
}

const renderedVirtuosoProps: MockVirtuosoProps[] = [];
const MockVirtuoso = React.forwardRef<unknown, MockVirtuosoProps>((props, _ref) => {
  if (props) {
    renderedVirtuosoProps.push(props);
  }
  const Header = props.components?.Header;
  const Footer = props.components?.Footer;
  return (
    <>
      {Header && props.context ? <Header context={props.context} /> : null}
      {Footer && props.context ? <Footer context={props.context} /> : null}
    </>
  );
});

// ChatPanel 通过 react-virtuoso 的 props 暴露渲染回调；测试替换列表容器，
// 只观察连续流式更新时的引用和 context，不让 DOM 测量掩盖组件行为。
mock.module("react-virtuoso", () => ({ Virtuoso: MockVirtuoso }));

const { default: ChatPanel } = await import("./ChatPanel");
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
  };
}

function latestVirtuosoProps(): MockVirtuosoProps {
  const props = renderedVirtuosoProps[renderedVirtuosoProps.length - 1];
  if (!props) throw new Error("测试未捕获 Virtuoso props");
  return props;
}

describe("ChatPanel Virtuoso 渲染引用", () => {
  test("流式更新和取消后重试保持回调引用，同时读取最新历史状态", () => {
    renderedVirtuosoProps.length = 0;
    const first = panelProps([conversation("第一段")]);
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<ChatPanel {...first} />);
    });
    const initialProps = latestVirtuosoProps();

    act(() => {
      renderer!.update(
        <ChatPanel
          {...first}
          conversations={[conversation("第一段继续流式增长") ]}
        />,
      );
    });
    const streamingProps = latestVirtuosoProps();
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
        <ChatPanel
          {...first}
          conversations={[conversation("取消后重试的流式正文") ]}
          hasOlderMessages
          loadingOlderMessages
          historyError="历史加载仍在进行"
          onRetryHistory={retry}
        />,
      );
    });
    const retryProps = latestVirtuosoProps();

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
