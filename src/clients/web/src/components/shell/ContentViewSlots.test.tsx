import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import type { SessionChangesSummary } from "../../types/backend";
import type { ConversationContentView } from "../../types/frontend";
import AgentStatePanel from "../panels/inspectionPanels/AgentStatePanel";
import EventQueuePanel from "../panels/inspectionPanels/EventQueuePanel";
import RequestLogPanel from "../panels/inspectionPanels/RequestLogPanel";
import ContentViewSlots from "./ContentViewSlots";

/** 主窗口会话区四个内容视图槽的编排契约：错误/骨架分支、槽位显隐与变更摘要优先级。 */

function summary(files: number): SessionChangesSummary {
  return { files, additions: 0, deletions: 0 };
}

type SlotOverrides = Partial<Parameters<typeof ContentViewSlots>[0]>;

function slotProps(overrides: SlotOverrides = {}): Parameters<typeof ContentViewSlots>[0] {
  const props: Parameters<typeof ContentViewSlots>[0] = {
    error: null,
    isBootstrapping: false,
    onRetryGatewayState: async () => {},
    contentView: "default",
    apiPort: 8010,
    workspaceId: "gw_demo",
    session: null,
    sessionId: "session-1",
    hasActiveSession: true,
    activeSessionCacheKey: "gw_demo:session-1",
    expandedDetails: false,
    agentStateJsonl: "",
    agentStateMessageCount: 0,
    agentStateLoadedAt: null,
    agentStateLoading: false,
    agentStateError: null,
    receivedEvents: [],
    activeTraceHistory: null,
    onLoadOlderTraceHistory: async () => 0,
    onRetryTraceHistory: async () => {},
    requestLogs: [],
    requestLogsLoading: false,
    requestLogsError: null,
    requestLogsLoadedAt: null,
    onRetryRequestLogs: () => {},
    conversations: [],
    activeTurnTimeline: null,
    changesHint: null,
    changesHintLoading: false,
    activeChangeset: null,
    gatewayUserViewStates: new Map(),
    onLoadOlderMessages: async () => {},
    onLoadNewerMessages: async () => {},
    onLoadAroundTurn: async () => {},
    onLoadTurnDetails: async () => {},
    onLoadAgentStateMessageRawContent: async () => "",
    onRetryHistory: () => {},
    onReplayTurn: async () => {},
    onRemovePending: async () => {},
    onChangePendingPolicy: async () => {},
    onViewStateChange: () => {},
    onViewStateRestoreStatus: () => {},
  };
  return { ...props, ...overrides };
}

function slot(overrides: SlotOverrides = {}) {
  return renderToStaticMarkup(<ContentViewSlots {...slotProps(overrides)} />);
}

/** 数出某个槽位外壳及其 hidden 状态，避免断言被相邻槽位串味。 */
function slotMarkup(html: string, index: number): string {
  const parts = html.split("<div class=\"content-view-slot");
  return parts[index + 1] ?? "";
}

describe("ContentViewSlots 内容视图编排契约", () => {
  test("初始化失败时优先展示错误出口，不渲染任何内容槽", () => {
    const html = slot({ error: "gateway 连接失败" });

    expect(html).toContain("前端初始化失败");
    expect(html).toContain("gateway 连接失败");
    expect(html).toContain("重新加载工作区");
    expect(html).not.toContain("content-view-slot");
    expect(html).not.toContain("bootstrap-state");
  });

  test("启动骨架分支取代内容槽", () => {
    const html = slot({ isBootstrapping: true });

    expect(html).toContain("正在加载工作区与会话...");
    expect(html).not.toContain("content-view-slot");
  });

  test("默认视图下只有会话记录槽可见，未访问的检视槽保留空隐藏外壳", () => {
    const html = slot({ contentView: "default" });

    expect(html).toContain("<div class=\"content-view-slot preserve-mounted-hidden\" hidden=\"\">");
    expect(slotMarkup(html, 0)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 1)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 2)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 3)).not.toContain("hidden=");
  });

  test("事件视图下只有事件槽可见，并透传 Trace 历史状态", () => {
    const html = slot({
      contentView: "events",
      activeTraceHistory: {
        scopeKey: "gw_demo:session-1",
        generation: 1,
        items: [],
        nextCursor: "cursor-9",
        hasMore: true,
        loading: false,
        loadingOlder: true,
        error: null,
      },
    });

    expect(slotMarkup(html, 0)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 1)).not.toContain("hidden=");
    expect(slotMarkup(html, 2)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 3)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 1)).toContain("正在加载面板...");
  });

  test("请求视图下只有请求槽可见", () => {
    const html = slot({ contentView: "requests" });

    expect(slotMarkup(html, 2)).not.toContain("hidden=");
    expect(slotMarkup(html, 0)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 1)).toContain("hidden=\"\"");
    expect(slotMarkup(html, 3)).toContain("hidden=\"\"");
  });

  test("检视面板首次访问时才挂载，访问后保留实例并按工作区会话键隔离", async () => {
    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(
        <ContentViewSlots
          {...slotProps({
            contentView: "default",
            workspaceId: "workspace-b",
            sessionId: "session-shared",
            activeSessionCacheKey: "workspace-b:session-shared",
          })}
        />,
      );
    });

    expect(renderer!.root.findAllByType(AgentStatePanel)).toHaveLength(0);
    expect(renderer!.root.findAllByType(EventQueuePanel)).toHaveLength(0);
    expect(renderer!.root.findAllByType(RequestLogPanel)).toHaveLength(0);

    await act(async () => {
      renderer!.update(
        <ContentViewSlots
          {...slotProps({
            contentView: "events",
            workspaceId: "workspace-b",
            sessionId: "session-shared",
            activeSessionCacheKey: "workspace-b:session-shared",
            activeTraceHistory: {
              scopeKey: "workspace-b:session-shared",
              generation: 1,
              items: [],
              nextCursor: null,
              hasMore: true,
              loading: false,
              loadingOlder: true,
              error: null,
            },
          })}
        />,
      );
    });

    expect(renderer!.root.findAllByType(EventQueuePanel)).toHaveLength(1);
    expect(renderer!.root.findAllByType(RequestLogPanel)).toHaveLength(0);
    expect(renderer!.root.findByType(EventQueuePanel).props.active).toBe(true);
    expect(renderer!.root.findByType(EventQueuePanel).props.historyHasMore).toBe(true);
    expect(renderer!.root.findByType(EventQueuePanel).props.historyLoadingOlder).toBe(true);

    await act(async () => {
      renderer!.update(
        <ContentViewSlots
          {...slotProps({
            contentView: "requests",
            workspaceId: "workspace-b",
            sessionId: "session-shared",
            activeSessionCacheKey: "workspace-b:session-shared",
          })}
        />,
      );
    });

    const eventProps = renderer!.root.findByType(EventQueuePanel).props;
    const requestProps = renderer!.root.findByType(RequestLogPanel).props;
    expect(renderer!.root.findAllByType(AgentStatePanel)).toHaveLength(0);
    expect(renderer!.root.findAllByType(EventQueuePanel)).toHaveLength(1);
    expect(renderer!.root.findAllByType(RequestLogPanel)).toHaveLength(1);
    expect(eventProps.active).toBe(false);
    expect(requestProps.active).toBe(true);
    expect(eventProps.scopeKey).toBe("workspace-b:session-shared");
    expect(eventProps.sessionId).toBe("session-shared");
    expect(requestProps.scopeKey).toBe("workspace-b:session-shared");
    expect(requestProps.sessionId).toBe("session-shared");

    await act(async () => {
      renderer!.update(
        <ContentViewSlots
          {...slotProps({
            contentView: "default",
            workspaceId: "workspace-b",
            sessionId: "session-shared",
            activeSessionCacheKey: "workspace-b:session-shared",
          })}
        />,
      );
    });

    expect(renderer!.root.findAllByType(EventQueuePanel)).toHaveLength(1);
    expect(renderer!.root.findAllByType(RequestLogPanel)).toHaveLength(1);
    await act(async () => renderer!.unmount());
  });

  test("三个检视视图隐藏会话记录槽，变更视图仍保留会话记录槽", () => {
    const inspectionViews: ConversationContentView[] = ["agent", "events", "requests"];
    for (const contentView of inspectionViews) {
      expect(slotMarkup(slot({ contentView }), 3)).toContain("hidden=\"\"");
    }

    // 变更视图属于会话记录一侧，会话记录槽必须保持可见。
    const changes = slot({ contentView: "changes" });
    expect(slotMarkup(changes, 3)).not.toContain("hidden=");
    expect(slotMarkup(changes, 0)).toContain("hidden=\"\"");
  });

  test("变更摘要优先取与当前会话匹配的提示，内容视图为变更时回退活动变更集", () => {
    const matching = slot({
      contentView: "default",
      changesHint: { sessionId: "session-1", summary: summary(3) },
    });
    expect(matching).toContain("本会话有 3 个文件变更待审查");

    // 提示属于其它会话时不得串用，此时按活动变更集回退。
    const staleHint = slot({
      contentView: "changes",
      changesHint: { sessionId: "session-other", summary: summary(3) },
      activeChangeset: { summary: summary(7) } as Parameters<
        typeof ContentViewSlots
      >[0]["activeChangeset"],
    });
    expect(staleHint).not.toContain("本会话有 3 个文件变更待审查");
    expect(staleHint).toContain("本会话有 7 个文件变更待审查");
  });

  test("非变更视图不消费活动变更集，只显示检查中提示", () => {
    const html = slot({
      contentView: "default",
      changesHintLoading: true,
      activeChangeset: { summary: summary(7) } as Parameters<
        typeof ContentViewSlots
      >[0]["activeChangeset"],
    });

    expect(html).not.toContain("本会话有 7 个文件变更待审查");
    expect(html).toContain("正在检查会话文件变更...");
  });
});
