import React from "react";
import type { TurnHistoryInclude } from "../../../api/sessionTurnHistory";
import {
  conversationModelUsage,
  conversationTokenUsage,
} from "../../../state/tokenUsage";
import {
  aggregateConversationEvents,
  buildPendingStatusItem,
  isLiveConversationView,
} from "../../../state/trace/traceAggregation";
import type { TimelineItem } from "../../../state/timelineTypes";
import {
  liveTimelineItemsToRenderItems,
  responsePartsToTimelineItems,
} from "../../../state/responseParts";
import type { ConversationView } from "../../../types/frontend";
import MarkdownContent from "../MarkdownContent";
import ResponseActionToolbar from "../ResponseActionToolbar";
import ThinkingSection from "../ThinkingSection";
import ToolRow from "../ToolRow";
import type { ChatTurnActions } from "./useChatTurnActions";

function ErrorPart({ item }: { item: Extract<TimelineItem, { kind: "trace" }> }) {
  const message = [item.payload.error, item.payload.message, item.payload.detail]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  return (
    <div className="chat-inline-error" role="alert">
      <span className="codicon codicon-error" aria-hidden="true" />
      <span>{message ?? "运行失败"}</span>
    </div>
  );
}

function CancelledPart({ userInitiated }: { userInitiated: boolean }) {
  return (
    <div className="chat-inline-cancelled" role="status">
      <span className="codicon codicon-debug-stop" aria-hidden="true" />
      <span>{userInitiated ? "已由用户中断" : "任务已取消"}</span>
    </div>
  );
}

function ResponsePart({
  item,
  showRawDetails,
}: {
  item: TimelineItem;
  showRawDetails: boolean;
}): React.ReactNode {
  if (item.kind === "aggregated_text" && item.partKind === "markdown") {
    return (
      <MarkdownContent
        value={item.text}
        className={item.active ? "is-streaming" : ""}
        streaming={item.active}
      />
    );
  }
  if (item.kind === "aggregated_tool") {
    return <ToolRow item={item} showRawDetails={showRawDetails} />;
  }
  if (
    item.kind === "trace"
    && ["job_cancelled", "session_interrupted"].includes(item.eventType)
  ) {
    return <CancelledPart userInitiated={item.eventType === "session_interrupted"} />;
  }
  if (
    item.kind === "trace"
    && ["error", "job_failed"].includes(item.eventType)
  ) {
    return <ErrorPart item={item} />;
  }
  return null;
}

type WorkItem =
  | Extract<TimelineItem, { kind: "aggregated_text" }>
  | Extract<TimelineItem, { kind: "aggregated_tool" }>;

type RenderGroup =
  | { kind: "work"; id: string; items: WorkItem[] }
  | { kind: "response"; id: string; item: TimelineItem };

function persistedWorkItems(conversation: ConversationView): WorkItem[] {
  return responseItemsForConversation(conversation)
    .filter((item) => item.kind !== "aggregated_text" || item.partKind !== "markdown" || item.id !== `${conversation.conversationId}:assistant-final`)
    .filter(
      (item): item is WorkItem =>
        item.kind === "aggregated_text" || item.kind === "aggregated_tool",
    );
}

function responseItemsForConversation(conversation: ConversationView): TimelineItem[] {
  const items = responsePartsToTimelineItems(
    (conversation.responseParts ?? []).filter((part) => part.kind !== "final_text"),
  );
  const assistantMessages = conversation.assistantMessages ?? [];
  const finalAssistantMessage = assistantMessages.reduce<NonNullable<ConversationView["assistantMessages"]>[number] | undefined>(
    (longest, candidate) => !longest || candidate.content.length > longest.content.length
      ? candidate
      : longest,
    undefined,
  );
  const finalText = finalAssistantMessage?.content?.trim() ?? "";
  if (!finalText) {
    return items;
  }

  const lastMarkdown = [...items].reverse().find(
    (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
      item.kind === "aggregated_text" && item.partKind === "markdown",
  );
  if (lastMarkdown?.text.trim() === finalText) {
    return items;
  }

  // Turn 摘要可能只带 assistantMessages.response_preview，没有 response_parts。
  // 将这个权威最终正文补成同一种 TimelineItem，避免完成后只剩用户消息。
  return [
    ...items,
    {
      kind: "aggregated_text",
      id: `${conversation.conversationId}:assistant-final`,
      text: finalAssistantMessage?.content ?? "",
      partKind: "markdown",
      active: false,
      timestamp: finalAssistantMessage?.created_at ?? null,
      eventCount: 1,
      rawEvents: [],
    },
  ];
}

function buildRenderGroups(items: TimelineItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  let finalMarkdownIndex = -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "aggregated_text" && item.partKind === "markdown") {
      finalMarkdownIndex = index;
      break;
    }
  }
  for (const [index, item] of items.entries()) {
    const isWork = item.kind === "aggregated_tool"
      || (item.kind === "aggregated_text"
                && (item.partKind === "reasoning" || index !== finalMarkdownIndex));
    if (!isWork) {
      groups.push({ kind: "response", id: item.id, item });
      continue;
    }
    const previous = groups[groups.length - 1];
    if (previous?.kind === "work") previous.items.push(item);
    else groups.push({ kind: "work", id: `work-${item.id}`, items: [item] });
  }
  return groups;
}

function formatActivityDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "耗时 —";
  if (durationMs < 1000) return `耗时 ${durationMs}ms`;
  return `耗时 ${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 0 : 1)}s`;
}

function activityStatsPreview(
  stats: ConversationView["activityStats"],
): string {
  if (!stats) return "消息统计不可用";
  const values = [formatActivityDuration(stats.duration_ms)];
  values.push(`消息 ${stats.message_count} 条`);
  return values.join(" · ");
}

function ActivityDetails({
  items,
  showRawDetails,
}: {
  items: WorkItem[];
  showRawDetails: boolean;
}): React.ReactNode {
  if (items.length === 0) {
    return <div className="chat-thinking-empty">没有可展开的中间消息</div>;
  }
  return items.map((item) => item.kind === "aggregated_tool" ? (
    <ToolRow key={item.id} item={item} showRawDetails={showRawDetails} />
  ) : (
    <MarkdownContent
      key={item.id}
      value={item.text}
    />
  ));
}

function TurnActivitySummary({
  conversation,
  items,
  showRawDetails,
  onLoadTurnDetails,
}: {
  conversation: ConversationView;
  items: WorkItem[];
  showRawDetails: boolean;
  onLoadTurnDetails?: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
  ) => Promise<void>;
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);
  const [loading, setLoading] = React.useState(false);
  const [loaded, setLoaded] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const turnId = conversation.turnId;

  const toggle = async () => {
    if (loading) return;
    if (open) {
      setOpen(false);
      return;
    }
    setError(null);
    if (!loaded && turnId && onLoadTurnDetails) {
      setLoading(true);
      try {
        await onLoadTurnDetails(
          [turnId],
          `turn-activity:${turnId}`,
          false,
          [
            "user",
            "text",
            "reasoning_detail",
            "encrypted_reasoning_meta",
            "tool_summary",
            "tool_call",
            "tool_result",
            "final_response",
          ],
        );
        setLoaded(true);
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      } finally {
        setLoading(false);
      }
    }
    setOpen(true);
  };

  return (
    <section className={`chat-thinking chat-turn-activity ${open ? "is-open" : "is-complete"}`}>
      <button
        type="button"
        className="chat-thinking-toggle"
        aria-expanded={open}
        aria-label={open ? "收起 Turn 中间消息" : "展开 Turn 中间消息"}
        onClick={() => void toggle()}
      >
        <span className="codicon codicon-check" aria-hidden="true" />
        <span className="chat-thinking-preview">
          {loading ? "正在加载中间消息…" : activityStatsPreview(conversation.activityStats)}
        </span>
        <span
          className={`codicon ${open ? "codicon-chevron-down" : "codicon-chevron-right"}`}
          aria-hidden="true"
        />
      </button>
      {open ? (
        <div className="chat-thinking-body">
          {error ? (
            <div className="chat-inline-error" role="alert">
              <span className="codicon codicon-error" aria-hidden="true" />
              <span>{error}</span>
            </div>
          ) : (
            <ActivityDetails items={items} showRawDetails={showRawDetails} />
          )}
        </div>
      ) : null}
    </section>
  );
}

export default function ChatTurnResponseBody({
  conversation,
  showRawDetails,
  isLastTurn,
  sessionBusy,
  actions,
  onLoadTurnDetails,
}: {
  conversation: ConversationView;
  showRawDetails: boolean;
  isLastTurn: boolean;
  sessionBusy: boolean;
  actions: ChatTurnActions;
  onLoadTurnDetails?: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
  ) => Promise<void>;
}): React.ReactNode {
  const running = isLiveConversationView(conversation)
    && (conversation.status === "running" || conversation.status === "queued");
  const summaryOnly = conversation.turnItemsView === "summary";
  const hasPersistedResponse = (conversation.responseParts?.length ?? 0) > 0
    || (conversation.assistantMessages?.length ?? 0) > 0;
  const hasUnifiedParts = !running && hasPersistedResponse;
  const parts = hasUnifiedParts
    ? responseItemsForConversation(conversation)
    : isLiveConversationView(conversation)
      ? liveTimelineItemsToRenderItems(
        aggregateConversationEvents(
          conversation.events,
          conversation.conversationId,
          running,
        ),
      )
      : [];
  const hasSessionInterrupted = parts.some((item) =>
    item.kind === "trace" && item.eventType === "session_interrupted"
  );
  const visibleParts = parts.filter((item) =>
    item.kind === "aggregated_text"
    || item.kind === "aggregated_tool"
    || (item.kind === "trace"
      && ["error", "job_failed", "job_cancelled", "session_interrupted"]
        .includes(item.eventType)
      && !(hasSessionInterrupted && item.eventType === "job_cancelled")),
  );
  const renderGroups = buildRenderGroups(visibleParts);
  const persistedWork = persistedWorkItems(conversation);
  const historyTurn = conversation.displayMode === "history" && Boolean(conversation.turnId);
  const activityItems = historyTurn
    ? persistedWork
    : [
      ...persistedWork,
      ...renderGroups
        .filter((group): group is Extract<RenderGroup, { kind: "work" }> => group.kind === "work")
        .flatMap((group) => group.items),
    ];
  const finalTextPart = [...visibleParts].reverse().find(
    (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
      item.kind === "aggregated_text" && item.partKind === "markdown",
  );
  const hasActiveWork = visibleParts.some((item) =>
    (item.kind === "aggregated_tool"
      || (item.kind === "aggregated_text" && item.partKind === "reasoning"))
    && item.active,
  );
  const hasStreamingMarkdown = visibleParts.some((item) =>
    item.kind === "aggregated_text" && item.partKind === "markdown" && item.active
  );
  const status = running && !hasActiveWork && !hasStreamingMarkdown
    ? buildPendingStatusItem(conversation)
    : null;
  const showResponseActions = !summaryOnly
    && !running
    && Boolean(finalTextPart);
  const failedByJob = isLastTurn
    && !summaryOnly
    && !actions.isInternalDisplayMessage
    && conversation.status === "error"
    && !conversation.events.some((event) =>
      ["job_cancelled", "session_interrupted"].includes(event.type)
    );

  return (
    <>
      {historyTurn ? (
        <TurnActivitySummary
          conversation={conversation}
          items={activityItems}
          showRawDetails={showRawDetails}
          onLoadTurnDetails={onLoadTurnDetails}
        />
      ) : null}
      {renderGroups.map((group) => group.kind === "work" ? (
        historyTurn ? null : (
          <ThinkingSection
            key={group.id}
            items={group.items}
            active={running && group.items.some((item) => item.active)}
            showRawDetails={showRawDetails}
          />
        )
      ) : (
        <ResponsePart
          key={group.id}
          item={group.item}
          showRawDetails={showRawDetails}
        />
      ))}
      {summaryOnly && !historyTurn ? (
        <div className="chat-turn-detail-loading" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>正在加载完整内容…</span>
        </div>
      ) : null}
      {status ? (
        <div className="chat-working" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>{status.title}</span>
          <span className="chat-working-detail">{status.detail}</span>
        </div>
      ) : null}
      {showResponseActions ? (
        <ResponseActionToolbar
          responseText={finalTextPart?.text ?? ""}
          tokenUsage={conversationTokenUsage(conversation)}
          modelUsage={conversationModelUsage(conversation)}
          canRegenerate={isLastTurn && !sessionBusy && !actions.isInternalDisplayMessage}
          onRegenerate={() => actions.setConfirmAction("regenerate")}
        />
      ) : null}
      {failedByJob ? (
        // TODO: 后续为失败轮次重试补齐重试策略、模型切换和参数选择；当前按原输入重试。
        <button
          type="button"
          className="chat-failed-retry-button"
          disabled={actions.actionRunning}
          onClick={() => actions.setConfirmAction("retry_failed")}
        >
          <span className="codicon codicon-refresh" aria-hidden="true" />
          重试失败轮次
        </button>
      ) : null}
      {actions.confirmAction ? (
        <div className="chat-turn-action-confirmation" role="group" aria-label="确认轮次操作">
          <div className="chat-turn-action-warning">
            将移除此消息之后的会话上下文，但不会撤销已产生的文件修改。
          </div>
          <div className="chat-request-edit-actions">
            <button
              type="button"
              disabled={actions.actionRunning}
              onClick={() => actions.setConfirmAction(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="primary"
              disabled={actions.actionRunning}
              onClick={() => void actions.executeReplay(actions.confirmAction!)}
            >
              {actions.actionRunning
                ? "正在执行..."
                : actions.confirmAction === "regenerate"
                  ? "确认重新生成"
                  : "确认重试"}
            </button>
          </div>
        </div>
      ) : null}
      {actions.actionError ? (
        <div className="chat-inline-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{actions.actionError}</span>
        </div>
      ) : null}
    </>
  );
}
