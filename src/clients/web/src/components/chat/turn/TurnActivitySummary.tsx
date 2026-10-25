import React from "react";
import type { ConversationView } from "../../../types/frontend";
import type { RenderGroup, WorkItem, ToolTimelineItem } from "../../../state/timeline/chatResponseGroups";
import { historicalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import { buildPendingStatusItem } from "../../../state/trace/traceAggregation";
import MarkdownContent from "../MarkdownContent";
import ToolRow from "../ToolRow";
import { errorMessage } from "../../../utils/errorMessage";
import type { LoadToolDetails, LoadTurnDetails } from "./types";
import type { TimelineItem } from "../../../state/timeline/timelineTypes";
import { ResponsePart } from "./responseStatusParts";

export function ForkItemButtons({ item, onForkItem }: {
  item: TimelineItem;
  onForkItem?: (itemId: string) => void;
}): React.ReactNode {
  if (!onForkItem) return null;
  const itemIds = [...new Set((item.responseSources ?? []).flatMap((part) =>
    part.source.item_id ? [part.source.item_id] : [],
  ))];
  return itemIds.map((itemId, index) => (
    <button key={itemId} type="button" className="chat-user-action-menu-item"
      data-fork-item-id={itemId}
      title={itemIds.length > 1 ? `从第 ${index + 1} 条消息创建分支` : "从此消息创建分支"}
      aria-label={itemIds.length > 1 ? `从第 ${index + 1} 条消息创建分支` : "从此消息创建分支"}
      onClick={() => onForkItem(itemId)}>
      <span className="codicon codicon-git-pull-request" aria-hidden="true" />
    </button>
  ));
}

export function formatActivityDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "耗时 —";
  if (durationMs < 1000) return `耗时 ${durationMs}ms`;
  return `耗时 ${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 0 : 1)}s`;
}

export function activityStatsPreview(
  stats: ConversationView["activityStats"],
): string {
  if (!stats) return "耗时 — · Item 计数同步中";
  const values = [formatActivityDuration(stats.duration_ms)];
  values.push(`Item ${stats.item_count} 项`);
  return values.join(" · ");
}

export function workItemStatusPreview(item: ToolTimelineItem): string {
  if (item.active) return `正在运行 ${item.toolName}`;
  if (item.incomplete) return `${item.toolName} 调用未完成`;
  if (item.outcomeUnknown) return `${item.toolName} 结果未知`;
  if (item.failed) return `${item.toolName} 执行失败`;
  return `已运行 ${item.toolName}`;
}

export function activityPreviewForItems(
  items: WorkItem[],
  stats: ConversationView["activityStats"],
): string {
  let latestTool: ToolTimelineItem | undefined;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind === "aggregated_tool") {
      latestTool = item;
      break;
    }
  }
  const statsPreview = activityStatsPreview(stats);
  return latestTool
    && !latestTool.active
    && !latestTool.incomplete
    && !latestTool.outcomeUnknown
    && !latestTool.failed
    ? `${workItemStatusPreview(latestTool)} · ${statsPreview}`
    : statsPreview;
}

export function ConversationWorkSummary({
  conversation,
  items,
  running,
}: {
  conversation: ConversationView;
  items: WorkItem[];
  running: boolean;
}): React.ReactNode {
  if (running) {
    if (!conversation.messageStream) return null;
    const status = buildPendingStatusItem(conversation);
    if (!status) return null;
    return (
      <div className="chat-working" role="status" data-status-kind="work-summary">
        <span className="codicon codicon-sync codicon-modifier-spin" aria-hidden="true" />
        <span>{status.title}</span>
        <span className="chat-working-detail">{status.detail}</span>
      </div>
    );
  }

  const preview = activityPreviewForItems(items, conversation.activityStats);
  return (
    <section className="chat-thinking chat-turn-activity is-complete" data-status-kind="work-summary">
      <div
        className="chat-thinking-toggle is-static"
        role="status"
        aria-label={`Turn 中间消息：${preview}`}
      >
        <span className="codicon codicon-check" aria-hidden="true" />
        <span className="chat-thinking-preview">{preview}</span>
      </div>
    </section>
  );
}

export function ActivityDetails({
  items,
  showRawDetails,
  onLoadToolDetails,
  onForkItem,
}: {
  items: WorkItem[];
  showRawDetails: boolean;
  onLoadToolDetails?: (toolCallId: string) => Promise<void>;
  onForkItem?: (itemId: string) => void;
}): React.ReactNode {
  if (items.length === 0) {
    return <div className="chat-thinking-empty">没有可展开的中间消息</div>;
  }
  const hasRedactedThinking = items.some(
    (item) => item.kind === "aggregated_text" && item.redacted === true,
  );
  return (
    <>
      {hasRedactedThinking ? (
        <div className="chat-thinking-notice" role="status">
          <span className="codicon codicon-lock" aria-hidden="true" />
          <span>部分思考内容已隐藏</span>
        </div>
      ) : null}
      {items.map((item) => <div key={item.id} className="chat-activity-item">{item.kind === "aggregated_tool" ? (
        <ToolRow
          key={item.id}
          item={item}
          showRawDetails={showRawDetails}
          onLoadDetails={onLoadToolDetails}
        />
      ) : (
        <MarkdownContent key={item.id} value={item.text} responseSources={item.responseSources ? JSON.stringify(item.responseSources) : undefined} />
      )}<ForkItemButtons item={item} onForkItem={onForkItem} /></div>)}
    </>
  );
}

export function TurnActivitySummary({
  conversation,
  groups,
  showRawDetails,
  onLoadTurnDetails,
  onLoadToolDetails,
  onForkItem,
}: {
  conversation: ConversationView;
  groups: RenderGroup[];
  showRawDetails: boolean;
  onLoadTurnDetails?: LoadTurnDetails;
  onLoadToolDetails?: LoadToolDetails;
  onForkItem?: (itemId: string) => void;
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const turnId = conversation.turnId;
  const boundaryStatus = historicalBoundaryStatus(conversation);
  const activityPreview = activityStatsPreview(conversation.activityStats);
  const displayPreview = boundaryStatus
    ? `${activityPreview} · ${boundaryStatus.title}`
    : activityPreview;
  const hasNoActivity = conversation.activityStats?.item_count === 0;
  const hasWork = groups.some((group) => group.kind === "work");

  const toggle = async () => {
    if (loading) return;
    if (open) {
      setOpen(false);
      return;
    }
    setError(null);
    // full 是 DTO 形状；include 过滤后的 metadata 仍可能没有活动正文。
    if (turnId && onLoadTurnDetails) {
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
      } catch (loadError) {
        setError(errorMessage(loadError));
      } finally {
        setLoading(false);
      }
    }
    setOpen(true);
  };

  return (
    <>
      <section
        className={`chat-thinking chat-turn-activity ${open ? "is-open" : "is-complete"}${boundaryStatus ? " has-boundary" : ""}`}
        data-status-kind={boundaryStatus?.kind}
        data-duration-ms={conversation.activityStats?.duration_ms ?? undefined}
        data-item-count={conversation.activityStats?.item_count ?? undefined}
      >
        {hasNoActivity ? (
          <div
            className="chat-thinking-toggle is-static"
            role="status"
            aria-label={`Turn 中间消息：${displayPreview}`}
          >
            <span
              className={`codicon ${boundaryStatus?.icon ?? "codicon-check"}`}
              aria-hidden="true"
            />
            <span className="chat-thinking-preview">{displayPreview}</span>
            {boundaryStatus?.detail ? (
              <span className="chat-working-detail">{boundaryStatus.detail}</span>
            ) : null}
          </div>
        ) : (
          <button
            type="button"
            className="chat-thinking-toggle"
            aria-expanded={open}
            aria-label={`${open ? "收起" : "展开"} Turn 中间消息：${displayPreview}`}
            onClick={() => void toggle()}
          >
            <span
              className={`codicon ${boundaryStatus?.icon ?? "codicon-check"}`}
              aria-hidden="true"
            />
            <span className="chat-thinking-preview">
              {displayPreview}
            </span>
            {loading ? <span className="chat-working-detail" role="status">正在加载中间消息…</span> : null}
            {boundaryStatus?.detail ? (
              <span className="chat-working-detail">{boundaryStatus.detail}</span>
            ) : null}
            <span
              className={`codicon ${open ? "codicon-chevron-down" : "codicon-chevron-right"}`}
              aria-hidden="true"
            />
          </button>
        )}
        {open && (error || !hasWork) ? (
          <div className="chat-thinking-body">
            {error ? (
              <div className="chat-inline-error" role="alert">
                <span className="codicon codicon-error" aria-hidden="true" />
                <span>{error}</span>
              </div>
            ) : (
              <ActivityDetails items={[]} showRawDetails={showRawDetails} />
            )}
          </div>
        ) : null}
      </section>
      {groups.map((group) => group.kind === "response" ? (
        <ResponsePart key={group.id} item={group.item} />
      ) : open && !error ? (
        <div key={group.id} className="chat-thinking-body">
          <ActivityDetails
            items={group.items}
            showRawDetails={showRawDetails}
            onForkItem={onForkItem}
            onLoadToolDetails={turnId && onLoadToolDetails
              ? (toolCallId) => onLoadToolDetails(turnId, toolCallId)
              : undefined}
          />
        </div>
      ) : null)}
    </>
  );
}
