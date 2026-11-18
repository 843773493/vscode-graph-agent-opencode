import { formatNumber, t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import { MessageSearchLocationContext } from "../messageSearchLocation";
import type { ConversationView } from "../../../types/frontend";
import type { RenderGroup, WorkItem, ToolTimelineItem } from "../../../state/timeline/chatResponseGroups";
import { historicalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import MarkdownContent from "../MarkdownContent";
import ToolRow from "../ToolRow";
import { errorMessage } from "../../../utils/errorMessage";
import type { LoadToolDetails, LoadTurnDetails } from "./types";
import { ResponsePart } from "./responseStatusParts";
import { responseItemMenuActions, responseItemTarget, useChatItemMenu } from "../itemContextMenu";

function ActivityItem({ item, showRawDetails, onLoadToolDetails, onForkItem }: {
  item: WorkItem;
  showRawDetails: boolean;
  onLoadToolDetails?: (toolCallId: string) => Promise<void>;
  onForkItem?: (itemId: string) => void;
}): React.ReactNode {
  const menu = useChatItemMenu(() => responseItemMenuActions(item, onForkItem), true, () => responseItemTarget(item));
  return <div className="chat-activity-item" data-chat-item-id={item.id} {...menu}>
    {item.kind === "aggregated_tool" ? (
      <ToolRow item={item} showRawDetails={showRawDetails} onLoadDetails={onLoadToolDetails} />
    ) : (
      <MarkdownContent value={item.text} responseSources={item.responseSources ? JSON.stringify(item.responseSources) : undefined} />
    )}
  </div>;
}

export function formatActivityDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return t("chat.activity.durationUnknown");
  if (durationMs < 1000) return t("chat.activity.ms", formatNumber(durationMs));
  const fractionDigits = durationMs >= 10_000 ? 0 : 1;
  return t("chat.activity.seconds", formatNumber(durationMs / 1000, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }));
}

export function activityStatsPreview(
  stats: ConversationView["activityStats"],
): string {
  if (!stats) return t("chat.activity.statsPending");
  const values = [formatActivityDuration(stats.duration_ms)];
  values.push(t("chat.activity.items", formatNumber(stats.item_count)));
  return values.join(" · ");
}

export function workItemStatusPreview(item: ToolTimelineItem): string {
  if (item.active) return t("chat.tool.running", item.toolName);
  if (item.incomplete) return t("chat.tool.incomplete", item.toolName);
  if (item.outcomeUnknown) return t("chat.tool.unknown", item.toolName);
  if (item.failed) return t("chat.tool.failed", item.toolName);
  return t("chat.tool.completed", item.toolName);
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
}: {
  conversation: ConversationView;
  items: WorkItem[];
}): React.ReactNode {
  const preview = activityPreviewForItems(items, conversation.activityStats);
  return (
    <section className="chat-thinking chat-turn-activity is-complete" data-status-kind="work-summary">
      <div
        className="chat-thinking-toggle is-static"
        role="status"
        aria-label={t("chat.activity.preview", preview)}
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
    return <div className="chat-thinking-empty">{t("chat.activity.empty")}</div>;
  }
  const hasRedactedThinking = items.some(
    (item) => item.kind === "aggregated_text" && item.redacted === true,
  );
  return (
    <>
      {hasRedactedThinking ? (
        <div className="chat-thinking-notice" role="status">
          <span className="codicon codicon-lock" aria-hidden="true" />
          <span>{t("chat.thinking.redacted")}</span>
        </div>
      ) : null}
      {items.map((item) => <ActivityItem key={item.id} item={item} showRawDetails={showRawDetails}
        onLoadToolDetails={onLoadToolDetails} onForkItem={onForkItem} />)}
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
  const { message, tool, item: locatedItem } = React.useContext(MessageSearchLocationContext);
  React.useEffect(() => {
    if (message?.role === "tool" && message.turn_id === turnId || tool?.turn_id === turnId || locatedItem?.turn_id === turnId) { setOpen(true); setError(null); }
  }, [message, tool, locatedItem, turnId]);
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
            "metadata",
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
            aria-label={t("chat.activity.preview", displayPreview)}
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
            aria-label={t(open ? "chat.thinking.collapse" : "chat.thinking.expand", displayPreview)}
            onClick={() => void toggle()}
          >
            <span
              className={`codicon ${boundaryStatus?.icon ?? "codicon-check"}`}
              aria-hidden="true"
            />
            <span className="chat-thinking-preview">
              {displayPreview}
            </span>
            {loading ? <span className="chat-working-detail" role="status">{t("chat.activity.loading")}</span> : null}
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
        <ResponsePart key={group.id} item={group.item} onForkItem={onForkItem} />
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
