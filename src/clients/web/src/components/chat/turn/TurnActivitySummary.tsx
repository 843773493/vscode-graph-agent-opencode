import { formatNumber, t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import { MessageSearchLocationContext } from "../messageSearchLocation";
import { isLiveConversationView } from "../../../state/trace/traceAggregation";
import type { ConversationView } from "../../../types/frontend";
import type { RenderGroup, WorkItem, ToolTimelineItem, HistoricalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import { historicalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import MarkdownContent from "../MarkdownContent";
import ToolRow from "../ToolRow";
import { errorMessage } from "../../../utils/errorMessage";
import type { LoadToolDetails, LoadTurnDetails } from "./types";
import { ResponsePart } from "./responseStatusParts";
import { responseItemMenuActions, responseItemTarget, useChatItemMenu } from "../itemContextMenu";
import ResponseUsage, { responseUsageDetails, type ResponseUsageProps } from "./ResponseUsage";

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

function activityStatsFields(stats: ConversationView["activityStats"]): [string, string] {
  return [formatActivityDuration(stats?.duration_ms), stats
    ? t("chat.activity.items", formatNumber(stats.item_count))
    : t("chat.activity.itemsPending")];
}

function ActivitySummaryHeader({ stats, usage, status, running = false, loading = false, open = false, onToggle }: {
  stats: ConversationView["activityStats"];
  usage?: ResponseUsageProps;
  status?: Pick<HistoricalBoundaryStatus, "title" | "detail" | "icon"> | null;
  running?: boolean;
  loading?: boolean;
  open?: boolean;
  onToggle?: () => void;
}): React.ReactNode {
  const [duration, items] = running && !stats
    ? [t("chat.activity.running"), null] : activityStatsFields(stats);
  const preview = [duration, items, status?.title].filter(Boolean).join(" · ");
  const details = [duration, items, status?.title, status?.detail,
    loading ? t("chat.activity.loading") : null, usage ? responseUsageDetails(usage) : null]
    .filter(Boolean).join("\n");
  const content = <>
    <span className={"codicon " + (loading || running ? "codicon-loading codicon-modifier-spin" : status?.icon ?? "codicon-check")} aria-hidden="true" />
    <span className="chat-activity-summary">
      <span className="chat-activity-duration">{duration}</span>
      {items ? <span className="chat-activity-items"> · {items}</span> : null}
      {status ? <span className="chat-activity-status">{status.title}</span> : null}
      {usage ? <ResponseUsage {...usage} /> : null}
      {loading ? <span className="chat-working-detail">{t("chat.activity.loading")}</span> : null}
      {status?.detail ? <span className="chat-working-detail">{status.detail}</span> : null}
    </span>
    {onToggle ? <span className={"codicon " + (open ? "codicon-chevron-down" : "codicon-chevron-right")} aria-hidden="true" /> : null}
  </>;
  return <div className="chat-turn-activity-header" title={details}>
    {onToggle ? <button type="button" className="chat-thinking-toggle" aria-expanded={open}
      aria-busy={loading || running} aria-description={details}
      aria-label={t(open ? "chat.thinking.collapse" : "chat.thinking.expand", preview)} onClick={onToggle}>
      {content}
    </button> : <div className="chat-thinking-toggle is-static" role="status" aria-busy={running}
      aria-label={t("chat.activity.preview", preview)} aria-description={details}>{content}</div>}
  </div>;
}

function ConversationWorkSummary({ conversation, items, usage, running }: {
  conversation: ConversationView;
  items: WorkItem[];
  usage?: ResponseUsageProps;
  running: boolean;
}): React.ReactNode {
  let latestTool: ToolTimelineItem | undefined;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "aggregated_tool") { latestTool = item; break; }
  }
  const status = !running && latestTool && !latestTool.active && !latestTool.failed
    && !latestTool.outcomeUnknown && !latestTool.incomplete ? {
    title: t("chat.tool.completed", latestTool.toolName),
    icon: "codicon-check",
  } : undefined;
  return <section className="chat-thinking chat-turn-activity is-complete" data-status-kind="work-summary">
    <ActivitySummaryHeader stats={conversation.activityStats} usage={usage} status={status} running={running} />
  </section>;
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
  usage,
  conversation,
  groups,
  live = false,
  showRawDetails,
  onLoadTurnDetails,
  onLoadToolDetails,
  onForkItem,
}: {
  conversation: ConversationView;
  groups: RenderGroup[];
  live?: boolean;
  usage?: ResponseUsageProps;
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
  const hasNoActivity = conversation.activityStats?.item_count === 0;
  const hasWork = groups.some((group) => group.kind === "work");
  const historyTurn = !live;
  const running = isLiveConversationView(conversation)
    && (conversation.status === "running" || conversation.status === "queued");
  const items = groups.flatMap((group) => group.kind === "work" ? group.items : []);
  const answer = [...groups].reverse().find((group) => group.kind === "response"
    && group.item.kind === "aggregated_text" && group.item.partKind === "markdown");
  const liveAnswerKey = React.useRef<string | null>(null);
  if (!historyTurn) liveAnswerKey.current = answer?.id ?? null;
  const showLiveSummary = Boolean(answer)
    || (!running && (items.length > 0 || Boolean(conversation.activityStats) || Boolean(usage)));


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
      {historyTurn ? <section
        className={`chat-thinking chat-turn-activity ${open ? "is-open" : "is-complete"}${boundaryStatus ? " has-boundary" : ""}`}
        data-status-kind={boundaryStatus?.kind}
        data-duration-ms={conversation.activityStats?.duration_ms ?? undefined}
        data-item-count={conversation.activityStats?.item_count ?? undefined}
      >
        <ActivitySummaryHeader stats={conversation.activityStats} usage={usage} status={boundaryStatus}
          loading={loading} open={open} onToggle={hasNoActivity ? undefined : () => void toggle()} />
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
      </section> : showLiveSummary ? (
        <ConversationWorkSummary conversation={conversation} items={items} usage={usage} running={running} />
      ) : null}
      {groups.map((group) => group.kind === "response" ? (
        // 只在终态接管最后的实时回答槽；新增实时段仍使用各自 ID，不转移展开状态。
        <ResponsePart key={historyTurn && group === answer ? liveAnswerKey.current ?? group.id : group.id} item={group.item} onForkItem={onForkItem} />
      ) : historyTurn && open && !error ? (
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
